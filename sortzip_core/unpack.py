"""Verify delivered volumes and publish flat, independently recoverable groups."""
from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from sortzip_core import archive_members, delivery_manifest as delivery, engine, file_journal
from sortzip_core.archive_tools import ArchiveTool, check_tool
from sortzip_core.disk_space import check_write
from sortzip_core.i18n import tr


def _error(path):
    return ValueError(tr('解包文件校验失败：{0}',path))


def _match(path,row,cancel):
    info = delivery.describe(path,cancel=cancel)
    if (info['size'],info['sha256'])!=(row['size'],row['sha256']):
        raise _error(path)


def _check_files(folder,rows,cancel):
    if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
        raise _error(folder)
    actual = {path.name:path for path in folder.iterdir()}
    if actual.keys()!=rows.keys():
        raise _error(folder)
    for name,row in rows.items():
        _match(actual[name],row,cancel)


def _wrong_password(tool,error):
    if tool=='winrar':
        return error.returncode==11
    if tool=='bandizip' and error.returncode==14:
        lines = [line.strip() for line in (error.stdout or '').splitlines() if line.lower().startswith('error')]
        return bool(lines) and all(line.lower()=='error: invalid password' or
                                   line.endswith('(0xa0000021: Invalid Password)') for line in lines)
    return False


def _seven_zip_password_failure(error,entry):
    if error.returncode!=2:
        return None
    lines = [line.strip() for line in (error.stderr or '').splitlines() if line.strip()]
    if lines and all(line.startswith('ERROR: Wrong password : ') for line in lines):
        return 'wrong'
    ambiguous_prefixes = ('ERROR: Data Error in encrypted file. Wrong password? : ',
                          'ERROR: CRC Failed in encrypted file. Wrong password? : ')
    if lines and all(line.startswith(ambiguous_prefixes+('ERROR: Wrong password : ',)) for line in lines):
        return 'ambiguous'
    if lines==[f'ERROR: {entry}','Cannot open encrypted archive. Wrong password?','ERRORS:','Headers Error']:
        return 'ambiguous'
    return None


def _format_matches(manifest,entry):
    signature = {'zip':(b'PK\x03\x04',b'PK\x07\x08',b'PK\x05\x06'),
                 '7z':(b"7z\xbc\xaf\x27\x1c",),
                 'rar':(b'Rar!\x1a\x07\x01\x00',)}[manifest['format']]
    if manifest['tool']=='bandizip' and manifest['format']=='zip':
        first = next((row['canonical'] for row in manifest['volumes'] if row['canonical'].endswith('.z01')),None)
        if first:
            entry = entry.parent/first
    with entry.open('rb') as stream:
        if not stream.read(8).startswith(signature):
            raise _error(entry)


def _members(tool,entry,password,cancel):
    command,encoding = tool.list_command(entry,password)
    output,_ = engine._run_proc(command,cancel,stdout_encoding=encoding,raw_stdout=True)
    names = archive_members._names(tool.identifier,output)
    delivery._unique(names)
    if delivery.MEMBER not in names:
        raise _error(entry)
    if tool.identifier=='bandizip':
        command[2] = '-list:v'
        output,_ = engine._run_proc(command,cancel,stdout_encoding=encoding,raw_stdout=True)
        lines = output.split('\n')
        rules = [index for index,line in enumerate(lines) if line.startswith('------------------- ')]
        if len(rules)!=2:
            raise _error(entry)
        rows = lines[rules[0]+1:rules[1]]
        if len(rows)!=len(names):
            raise _error(entry)
        sizes = {}
        for name,row in zip(names,rows,strict=True):
            prefix = row.removesuffix(name)
            match = re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ([A_ZDHRSP_]+)\s+([\d,]+)\s+[\d,]+\s',prefix)
            if (prefix==row or not match or 'D' in match[1]
                    or re.search(r'\s[lbcps][rwxstST-]{9}\s',prefix)):
                raise _error(entry)
            sizes[name] = int(match[2].replace(',',''))
    elif tool.identifier=='7zip':
        sizes = {}
        for block in output.strip('\n').split('\n\n'):
            fields = dict(line.split(' = ',1) for line in block.split('\n') if ' = ' in line)
            if re.search(r'\b[lbcps][rwxstST-]{9}\b',fields.get('Attributes','')):
                raise _error(entry)
            size = fields.get('Size','')
            if not size.isascii() or not size.isdecimal() or len(size)>20:
                raise _error(entry)
            sizes[fields['Path']] = int(size)
    else:
        sizes = {}
        for block in output.strip('\n').split('\n\n'):
            fields = dict(line.strip().split(': ',1) for line in block.split('\n') if ': ' in line)
            if 'Name' in fields:
                size = fields.get('Size','')
                if not size.isascii() or not size.isdecimal() or len(size)>20:
                    raise _error(entry)
                size = int(size)
                if sizes.setdefault(fields['Name'],size)!=size:
                    raise _error(entry)
    if sizes.keys()!=set(names) or any(size<0 for size in sizes.values()) or sizes[delivery.MEMBER]>delivery.MAX_MANIFEST:
        raise _error(entry)
    return sizes


class Unpack:
    def __init__(self,config,progress,cancel,force,prompt,stats):
        self.destination = Path(config['dest']).resolve()
        self.keep = config.get('unpack_keep',False)
        self.original = config.get('unpack_original',True)
        self.passwords = [password for password in config.get('unpack_passwords',()) if password]
        self.tools = {}
        self.tool_paths = config.get('archive_tool_paths',{})
        self.progress,self.cancel,self.force,self.prompt = progress,cancel,force,prompt
        self.stats = stats
        self.completed_work = []

    def check(self):
        if self.cancel and self.cancel():
            raise file_journal.Cancelled()

    def password(self,tool,entry,manifest,layer):
        if not manifest['encrypted']:
            return 'SortZip-unencrypted-only'
        tried = set()
        ambiguous = False
        for value in self.passwords:
            if value in tried:
                continue
            tried.add(value)
            result = self.try_password(tool,entry,value)
            if result is True:
                return value
            ambiguous |= result is None
        while self.prompt is not None:
            self.check()
            value = self.prompt(layer,manifest['entry'],ambiguous)
            if value is None:
                raise file_journal.Cancelled()
            if not value or value in tried:
                continue
            tried.add(value)
            result = self.try_password(tool,entry,value)
            if result is True:
                self.passwords.append(value)
                return value
            ambiguous |= result is None
        if ambiguous:
            raise ValueError(tr('已试完密码清单。密码可能不符，也可能是归档数据异常：{0}',manifest['entry']))
        raise ValueError(tr('需要解包密码：{0}',manifest['entry']))

    def try_password(self,tool,entry,password):
        """Return True on success, False on rejection, or None for an ambiguous 7-Zip error."""
        self.check()
        if (tool.identifier!='bandizip' and '"' in password) or (tool.identifier=='winrar' and password=='-'):
            return False
        try:
            engine._run_proc(tool.test_command(entry,password),self.force)
            return True
        except subprocess.CalledProcessError as error:
            if tool.identifier=='7zip':
                failure = _seven_zip_password_failure(error,entry)
                if failure:
                    return False if failure=='wrong' else None
            if _wrong_password(tool.identifier,error):
                return False
            raise ValueError(tr('工具无法确认是密码错误，已暂停解包：{0}',entry.name)) from error

    def open_delivery(self,path):
        path = Path(path).absolute()
        if not path.name.lower().endswith(delivery.EXTENSION):
            raise ValueError(tr('请选择 SortZip 交付清单'))
        manifest = delivery.read(path,delivery.validate_archive)
        label = delivery.name(path.name[:-len(delivery.EXTENSION)])
        if label.casefold().startswith('.sortzip-'):
            raise _error(path)
        key = sha256(delivery.encode(manifest)).hexdigest()
        label = label[:120].rstrip(' .')+'-'+key[:12]
        self.destination.mkdir(parents=True,exist_ok=True)
        result = file_journal.safe_path(self.destination,label)
        work_key = sha256((key+label.casefold()).encode()).hexdigest()
        work = file_journal.safe_path(self.destination,'.sortzip-unpack-'+work_key[:24])
        checkpoint = file_journal.safe_path(work,'unpack.json')
        if file_journal.exists(result) and not file_journal.exists(checkpoint):
            raise ValueError(tr('解包目标已存在：{0}',result))
        work.mkdir(exist_ok=True)
        if work.is_junction() or work.is_symlink():
            raise _error(work)
        with file_journal.task_lock(work):
            options = {'schema':1,'key':key,'result':label,'keep':self.keep,'original':self.original}
            ready = file_journal.safe_path(work,'publish.json')
            if file_journal.exists(result) and not file_journal.exists(ready):
                raise ValueError(tr('解包目标已存在：{0}',result))
            if file_journal.exists(checkpoint):
                file_journal.identity(checkpoint)
                saved = delivery.read(checkpoint,lambda data:data)
                if (not isinstance(saved,dict) or saved.keys()!=options.keys()
                        or saved['schema']!=1 or saved['key']!=key or saved['result']!=label
                        or type(saved['keep']) is not bool or type(saved['original']) is not bool):
                    raise _error(checkpoint)
                options = saved
            else:
                delivery.write_new(checkpoint,options)
            self.work,self.options = work,options
            staged_result = file_journal.safe_path(work,'result')
            if file_journal.exists(result):
                file_journal.identity(ready)
                if ready.read_bytes()!=delivery.encode({'key':key}):
                    raise _error(ready)
                if file_journal.exists(staged_result):
                    raise ValueError(tr('解包目标已存在：{0}',result))
                self.result = result
                self.published_paths = set()
                self.check_result(self.receipt(manifest,'1'),result/'group-1','1')
                self.check_tree()
            else:
                self.result = staged_result
                self.result.mkdir(exist_ok=True)
                self.group(manifest,path.parent,'1',None,source=True)
                self.published_paths = set()
                self.check_result(self.receipt(manifest,'1'),self.result/'group-1','1')
                self.check_tree()
                self.check()
                delivery.write_new(ready,{'key':key})
                _publish_directory(self.result,result)
        self.completed_work.append(work)
        self.stats['groups'] += 1

    def check_tree(self):
        if not self.result.is_dir() or self.result.is_symlink() or self.result.is_junction():
            raise _error(self.result)
        archives = file_journal.safe_path(self.result,'_archives')
        if self.options['keep'] and archives.is_dir():
            if archives.is_junction():
                raise _error(archives)
            self.published_paths.add(archives)
            if set(archives.iterdir())!={path for path in self.published_paths if path.parent==archives}:
                raise _error(archives)
        if set(self.result.iterdir())!={path for path in self.published_paths if path.parent==self.result}:
            raise _error(self.result)

    def cleanup_completed(self):
        for work in self.completed_work:
            try:
                with file_journal.task_lock(work):
                    for receipt in work.glob('group-*.json'):
                        if re.fullmatch(r'group-[0-9-]+\.json',receipt.name):
                            receipt.unlink()
                    for artifact in work.iterdir():
                        if re.fullmatch(r'(extract|publish)-[a-z0-9_]{8}',artifact.name):
                            _remove_scratch(artifact)
                    for metadata in ('unpack.json','publish.json'):
                        (work/metadata).unlink(missing_ok=True)
                (work/file_journal.LOCK).unlink(missing_ok=True)
                work.rmdir()
            except (OSError,ValueError):
                print(tr('解包已完成，工作目录尚未清理：{0}',work))

    def receipt(self,manifest,key):
        path = file_journal.safe_path(self.work,'group-'+key+'.json')
        data = delivery.read(path,lambda data:data)
        if not isinstance(data,dict) or data.get('identity')!=sha256(delivery.encode(manifest)).hexdigest():
            raise _error(path)
        return delivery.validate_members(data.get('inner'))

    def group(self,manifest,source_folder,key,layer,*,source=False):
        self.check()
        if key.count('-')>=128:
            raise _error(source_folder)
        receipt = file_journal.safe_path(self.work,'group-'+key+'.json')
        staged = file_journal.safe_path(self.work,'stage-'+key)
        marker = file_journal.safe_path(self.work,'verified-'+key+'.json')
        kept = file_journal.safe_path(self.work,'result/_archives/'+key)
        restored = file_journal.safe_path(self.work,'result/group-'+key)
        identity = sha256(delivery.encode(manifest)).hexdigest()
        if file_journal.exists(receipt):
            inner = self.receipt(manifest,key)
            if layer is not None and inner['layer']!=layer:
                raise _error(receipt)
            self.check_result(inner,restored,key,record_paths=False)
            if not source:
                self.release(manifest,source_folder,kept)
            self.remove_stage(inner,staged)
            marker.unlink(missing_ok=True)
            return
        if file_journal.exists(marker):
            saved = delivery.read(marker,lambda data: data)
            if not isinstance(saved,dict) or saved.get('identity')!=identity:
                raise _error(marker)
            inner = delivery.validate_members(saved.get('inner'))
        else:
            if file_journal.exists(staged):
                # A crash may have published the stage before the marker.
                inner = delivery.read(staged/delivery.MEMBER,delivery.validate_members)
                _check_files(staged,self.expected(inner,staged/delivery.MEMBER),self.cancel)
            else:
                inner = self.extract(manifest,source_folder,staged,layer)
            delivery.write_new(marker,dict(identity=identity,inner=inner))
        if layer is not None and inner['layer']!=layer:
            raise _error(staged)
        self.check()
        if inner['layer']==1:
            self.publish_group(inner,staged,restored)
        else:
            for index,child in enumerate(inner['children'],1):
                if child['tool']!=manifest['tool']:
                    raise _error(staged)
                self.group(child,staged,f'{key}-{index}',inner['layer']-1)
        delivery.write_new(receipt,dict(identity=identity,inner=inner))
        if not source:
            self.release(manifest,source_folder,kept)
        self.remove_stage(inner,staged)
        marker.unlink(missing_ok=True)

    def check_result(self,inner,folder,key,*,record_paths=True):
        if inner['layer']==1:
            _check_files(folder,self.targets(inner),self.cancel)
            if record_paths:
                self.published_paths.add(folder)
        else:
            for index,child in enumerate(inner['children'],1):
                child_key = f'{key}-{index}'
                node = self.receipt(child,child_key)
                if node['layer']!=inner['layer']-1:
                    raise _error(self.work)
                folder = file_journal.safe_path(self.result,'group-'+child_key)
                self.check_result(node,folder,child_key,record_paths=record_paths)
                if self.options['keep']:
                    kept = file_journal.safe_path(self.result,'_archives/'+child_key)
                    _check_files(kept,{row['shown']:row for row in child['volumes']},self.cancel)
                    if record_paths:
                        self.published_paths.add(kept)

    def release(self,manifest,folder,kept):
        if file_journal.exists(folder) and (not folder.is_dir() or folder.is_symlink() or folder.is_junction()):
            raise _error(folder)
        if self.options['keep']:
            kept.parent.mkdir(exist_ok=True)
            kept.mkdir(exist_ok=True)
        for row in manifest['volumes']:
            path = file_journal.safe_path(folder,row['shown'])
            if self.options['keep']:
                target = file_journal.safe_path(kept,row['shown'])
                if not file_journal.exists(target):
                    _match(path,row,self.cancel)
                    file_journal.rename_new(path,target)
                else:
                    _match(target,row,self.cancel)
                    if file_journal.exists(path):
                        raise _error(path)
            elif file_journal.exists(path):
                _match(path,row,self.cancel)
                path.unlink()
                file_journal._sync_dir(path.parent)

    def remove_stage(self,inner,staged):
        if not file_journal.exists(staged):
            return
        if not staged.is_dir() or staged.is_symlink() or staged.is_junction():
            raise _error(staged)
        for row in inner['files']:
            path = file_journal.safe_path(staged,row['name'])
            if file_journal.exists(path):
                _match(path,row,self.cancel)
                path.unlink()
        (staged/delivery.MEMBER).unlink(missing_ok=True)
        if staged.exists():
            staged.rmdir()

    def targets(self,inner):
        return {row.get('original',row['name']) if self.options['original'] else row['name']:row
                for row in inner['files'] if not row['auxiliary']}

    def publish_group(self,inner,staged,restored):
        targets = self.targets(inner)
        if file_journal.exists(restored):
            _check_files(restored,targets,self.cancel)
            return
        check_write(self.work,sum(row['size'] for row in targets.values()),tr('解包并恢复'))
        with TemporaryDirectory(prefix='publish-',dir=self.work) as temp:
            folder = Path(temp)/'files'
            folder.mkdir()
            for index,(name,row) in enumerate(targets.items()):
                source = file_journal.safe_path(staged,row['name'])
                temporary = Path(temp)/str(index)
                self.copy(source,temporary,row)
            # Separate staging and final naming so swaps and case-only changes are harmless.
            for index,name in enumerate(targets):
                file_journal.rename_new(Path(temp)/str(index),folder/name)
            _check_files(folder,targets,self.cancel)
            _publish_directory(folder,restored)
        self.stats['files_moved'] += len(targets)

    def copy(self,source,target,row):
        file_journal.identity(source)
        with source.open('rb') as incoming,target.open('xb') as output:
            remaining = row['size']
            while block := incoming.read(min(1024*1024,remaining+1)):
                self.check()
                if len(block)>remaining:
                    raise _error(source)
                remaining -= len(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        _match(target,row,self.cancel)

    def expected(self,inner,manifest_path):
        info = delivery.describe(manifest_path,cancel=self.cancel)
        rows = {delivery.MEMBER:info}
        if inner['layer']==1:
            rows.update((row['name'],row) for row in inner['files'])
        else:
            for child in inner['children']:
                rows.update((row['shown'],row) for row in child['volumes'])
        return rows

    def extract(self,manifest,source_folder,staged,layer):
        tool_id = manifest['tool']
        if tool_id not in self.tools:
            self.tools[tool_id] = ArchiveTool(tool_id,check_tool(tool_id,self.tool_paths))
        tool = self.tools[tool_id]
        if self.progress:
            self.progress(0,100,self.stats['groups'],self.stats['groups_total'],
                          tr('正在解包：{0}',manifest['entry']))
        with TemporaryDirectory(prefix='extract-',dir=self.work) as temp:
            base = Path(temp)
            volumes,metadata,payload = (base/name for name in ('volumes','metadata','payload'))
            for folder in (volumes,metadata,payload):
                folder.mkdir()
            check_write(volumes,sum(row['size'] for row in manifest['volumes']),tr('解包并恢复'))
            entry = None
            for row in manifest['volumes']:
                source = file_journal.safe_path(source_folder,row['shown'])
                if not file_journal.exists(source):
                    raise ValueError(tr('缺少交付分卷：{0}',source))
                target = volumes/row['canonical']
                self.copy(source,target,row)
                if row['shown']==manifest['entry']:
                    entry = target
            _format_matches(manifest,entry)
            password = self.password(tool,entry,manifest,layer)
            self.check()
            sizes = _members(tool,entry,password,self.force)
            check_write(payload,sum(sizes.values())+sizes[delivery.MEMBER],tr('解包并恢复'))
            engine._run_proc(tool.extract_command(entry,metadata,password,delivery.MEMBER),self.force)
            inner_path = metadata/delivery.MEMBER
            if {item.name for item in metadata.iterdir()}!={delivery.MEMBER}:
                raise _error(metadata)
            inner = delivery.read(inner_path,delivery.validate_members)
            expected = self.expected(inner,inner_path)
            if {name:row['size'] for name,row in expected.items()}!=sizes:
                raise _error(entry)
            self.check()
            engine._run_proc(tool.extract_command(entry,payload,password),self.force)
            _check_files(payload,expected,self.cancel)
            for path in payload.iterdir():
                with path.open('r+b') as stream:
                    os.fsync(stream.fileno())
            _publish_directory(payload,staged)
        return inner


def _publish_directory(source,target):
    if file_journal.exists(target):
        raise ValueError(tr('解包目标已存在：{0}',target))
    if os.name=='nt':
        file_journal.rename_new(source,target)
    else:
        source.rename(target)
        file_journal._sync_dir(target.parent)


def _remove_scratch(folder):
    if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
        raise _error(folder)
    for path in folder.iterdir():
        if path.is_dir():
            _remove_scratch(path)
        else:
            file_journal.identity(path)
            path.unlink()
    folder.rmdir()


def run(config,stats,progress=None,cancel=None,force=None,prompt=None):
    paths = config['unpack_manifests']
    stats.update(groups=0,groups_total=len(paths),files_moved=0,unpack_task=True)
    task = Unpack(config,progress,cancel,force,prompt,stats)
    try:
        for path in paths:
            task.open_delivery(path)
        task.cleanup_completed()
        return 'completed'
    except (file_journal.Cancelled,engine.ForceCancelled):
        return engine._cancel_status(force)
    except subprocess.CalledProcessError as error:
        raise ValueError(tr('解包工具运行失败（退出码 {0}）',error.returncode)) from error
    finally:
        task.passwords.clear()
