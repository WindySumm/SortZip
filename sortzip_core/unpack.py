"""Verify delivered volumes and publish flat, independently recoverable groups."""
from hashlib import sha256
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from sortzip_core import archive_members, delivery_manifest as delivery, engine, file_journal
from sortzip_core.archive_tools import ArchiveTool, check_tool
from sortzip_core.disk_space import check_write
from sortzip_core.i18n import current_language, tr


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


def _members(tool,entry,password,cancel,*,embedded=True):
    command,encoding = tool.list_command(entry,password)
    output,_ = engine._run_proc(command,cancel,stdout_encoding=encoding,raw_stdout=True)
    names = archive_members._names(tool.identifier,output)
    delivery._unique(names)
    if embedded and delivery.MEMBER not in names:
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
    if sizes.keys()!=set(names) or any(size<0 for size in sizes.values()) or (
            embedded and sizes[delivery.MEMBER]>delivery.MAX_MANIFEST):
        raise _error(entry)
    return sizes


def _valid_retention(value,count):
    if count is None:
        return type(value) is bool
    return (isinstance(value,list) and all(type(layer) is int and 1<=layer<count for layer in value)
            and len(value)==len(set(value)))


def _unique_label(stem,taken):
    stem = _label_base(stem)
    candidate = stem
    index = 2
    while candidate.casefold() in taken:
        candidate = f'{stem} ({index})'
        index += 1
    return candidate


def _label_base(stem):
    prefix = delivery.name(stem)[:120].rstrip(' .')
    if not prefix:
        prefix = 'Archive'
    try:
        return delivery.name(prefix)
    except ValueError:
        return delivery.name(prefix[:119].rstrip(' .')+'-')


class Unpack:
    def __init__(self,config,progress,cancel,force,prompt,stats):
        self.destination = Path(config['dest']).resolve()
        self.keep = config.get('unpack_keep',False)
        self.retention = config.get('unpack_retention',{})
        self.manifest_keys = config.get('unpack_manifest_keys',{})
        self.original = config.get('unpack_original',True)
        self.passwords = [password for password in config.get('unpack_passwords',()) if password]
        self.tools = {}
        self.tool_paths = config.get('archive_tool_paths',{})
        self.progress,self.cancel,self.force,self.prompt = progress,cancel,force,prompt
        self.stats = stats
        self.completed_work = []
        self.shared_passwords = {}

    def check(self):
        if self.cancel and self.cancel():
            raise file_journal.Cancelled()

    def password(self,tool,entry,manifest,layer):
        if not manifest['encrypted']:
            return 'SortZip-unencrypted-only'
        tried = set()
        ambiguous = False
        preferred = self.shared_passwords.get(str(layer-1)) if layer else None
        for value in ([preferred,*self.passwords] if preferred else self.passwords):
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
        selection = str(path)
        path = Path(path).absolute()
        if not path.name.lower().endswith(delivery.EXTENSION):
            raise ValueError(tr('请选择 SortZip 交付清单'))
        manifest = delivery.read(path,delivery.validate_outer)
        label = delivery.name(path.name[:-len(delivery.EXTENSION)])
        if label.casefold().startswith('.sortzip-'):
            raise _error(path)
        key = sha256(delivery.encode(manifest)).hexdigest()
        if selection in self.manifest_keys and key!=self.manifest_keys[selection]:
            raise ValueError(tr('解包清单已更改，请重新选择：{0}',path))
        layer_count = manifest.get('layer_count')
        if selection in self.retention:
            keep = self.retention[selection]
        elif layer_count is None:
            keep = self.keep
        else:
            keep = list(range(1,layer_count)) if self.keep else []
        if not _valid_retention(keep,layer_count):
            raise ValueError(tr('解包清单的保留层设置无效：{0}',path))
        legacy_label = label[:120].rstrip(' .')+'-'+key[:12]
        self.destination.mkdir(parents=True,exist_ok=True)
        work,options = self.open_workspace(path,key,label,legacy_label,keep,layer_count)
        result = file_journal.safe_path(self.destination,options['result'])
        with file_journal.legacy_workspace_lock(work):
            checkpoint = file_journal.safe_path(work,'unpack.json')
            if delivery.read(checkpoint,lambda data:data)!=options:
                raise _error(checkpoint)
            ready = file_journal.safe_path(work,'publish.json')
            if file_journal.exists(result) and not file_journal.exists(ready):
                raise ValueError(tr('解包目标已存在：{0}',result))
            self.work,self.options = work,options
            self.manifest_path = path
            staged_result = file_journal.safe_path(work,'result')
            if file_journal.exists(result):
                file_journal.identity(ready)
                if ready.read_bytes()!=delivery.encode({'key':key}):
                    raise _error(ready)
                if file_journal.exists(staged_result):
                    raise ValueError(tr('解包目标已存在：{0}',result))
                self.result = result
                self.published_paths = set()
                inner = self.receipt(manifest,'1')
                self.check_layer(inner,layer_count)
                self.check_result(manifest,inner,'1')
                self.check_tree()
            else:
                self.result = staged_result
                self.result.mkdir(exist_ok=True)
                self.published_paths = set()
                self.group(manifest,path.parent,'1',layer_count,source=True)
                self.published_paths = set()
                self.check_result(manifest,self.receipt(manifest,'1'),'1')
                self.check_tree()
                self.check()
                delivery.write_new(ready,{'key':key})
                _publish_directory(self.result,result)
        self.completed_work.append(work)
        self.stats['groups'] += 1

    def open_workspace(self,path,key,label,legacy_label,keep,layer_count):
        legacy_key = sha256((key+legacy_label.casefold()).encode()).hexdigest()
        work_key = sha256((key+'\0'+os.path.normcase(str(path))).encode()).hexdigest()
        legacy = file_journal.safe_path(self.destination,'.sortzip-unpack-'+legacy_key[:24])
        work = file_journal.safe_path(self.destination,'.sortzip-unpack-'+work_key[:24])
        if file_journal.exists(legacy/'unpack.json') and not file_journal.exists(work/'unpack.json'):
            work = legacy
        if work.is_junction() or work.is_symlink():
            raise _error(work)
        checkpoint = file_journal.safe_path(work,'unpack.json')
        if file_journal.exists(checkpoint):
            options = delivery.read(checkpoint,lambda data:data)
            if (not isinstance(options,dict) or options.get('key')!=key
                    or not _valid_retention(options.get('keep'),layer_count)
                    or type(options.get('original')) is not bool):
                raise _error(checkpoint)
            if work==legacy:
                if options.get('schema')!=(1 if layer_count is None else 2) or options.get('result')!=legacy_label:
                    raise _error(checkpoint)
            elif (options.get('schema')!=3 or options.get('language') not in ('zh','en')
                    or not self.valid_result_label(options.get('result'),label)):
                raise _error(checkpoint)
            return work,options
        taken = {item.name.casefold() for item in self.destination.iterdir()}
        for folder in self.destination.glob('.sortzip-unpack-*'):
            if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
                continue
            saved = folder/'unpack.json'
            if file_journal.exists(saved):
                try:
                    other = delivery.read(saved,lambda data:data)
                except FileNotFoundError:
                    continue
                if isinstance(other,dict) and isinstance(other.get('result'),str):
                    taken.add(other['result'].casefold())
        name = _unique_label(label,taken)
        options = dict(schema=3,key=key,result=name,keep=keep,original=self.original,
                       language=current_language())
        work.mkdir(exist_ok=True)
        delivery.write_new(checkpoint,options)
        return work,options

    @staticmethod
    def valid_result_label(name,stem):
        stem = _unique_label(stem,set())
        return isinstance(name,str) and (name==stem or re.fullmatch(
            re.escape(stem)+r' \((?:[2-9]|[1-9][0-9]+)\)',name))

    def retained_folder(self):
        return {'zh':'保留的压缩包','en':'Retained archives'}.get(self.options.get('language'),'_archives')

    def named_group(self,manifest,key,layer,*,retained=False,create=False):
        if self.options['schema']!=3:
            relative = '_archives/'+key if retained else 'group-'+key
            return file_journal.safe_path(self.result,relative)
        base = self.result
        if retained:
            segment = f'第 {layer} 层' if self.options['language']=='zh' else f'Layer {layer}'
            base = file_journal.safe_path(self.result,self.retained_folder()+'/'+segment)
        marker = file_journal.safe_path(self.work,('kept-' if retained else 'name-')+key+'.json')
        if file_journal.exists(marker):
            data = delivery.read(marker,lambda data:data)
            if (not isinstance(data,dict) or not self.valid_result_label(data.get('name'),manifest['stem'])
                    or not retained and data['name'].casefold()==self.retained_folder().casefold()):
                raise _error(marker)
            name = data['name']
        elif create:
            taken = {item.name.casefold() for item in base.iterdir()} if base.is_dir() else set()
            if not retained:
                taken.add(self.retained_folder().casefold())
            name = _unique_label(manifest['stem'],taken)
            delivery.write_new(marker,dict(name=name))
        else:
            raise _error(marker)
        return file_journal.safe_path(self.result,(base/name).relative_to(self.result).as_posix())

    def check_tree(self):
        if not self.result.is_dir() or self.result.is_symlink() or self.result.is_junction():
            raise _error(self.result)
        parents = {self.result}
        for path in tuple(self.published_paths):
            parents.update(parent for parent in path.parents if parent.is_relative_to(self.result))
        children = {parent:set() for parent in parents}
        for path in (self.published_paths|parents)-{self.result}:
            children[path.parent].add(path)
        for parent,expected in children.items():
            if parent.is_symlink() or parent.is_junction() or set(parent.iterdir())!=expected:
                raise _error(parent)

    def cleanup_completed(self):
        for work in self.completed_work:
            try:
                if not work.is_dir() or work.is_junction() or work.is_symlink():
                    continue
                with file_journal.legacy_workspace_lock(work):
                    for receipt in work.glob('*.json'):
                        if re.fullmatch(r'(?:group|name|kept)-[0-9-]+\.json',receipt.name):
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

    def check_layer(self,inner,layer):
        if layer is not None and inner['layer']!=layer:
            raise ValueError(tr('解包清单 {0} 声明第 {1} 层，实际为第 {2} 层。',
                                self.manifest_path,layer,inner['layer']))

    def retain_layer(self,layer):
        keep = self.options['keep']
        return keep if type(keep) is bool else layer in keep

    def group(self,manifest,source_folder,key,layer,*,source=False):
        self.check()
        if key.count('-')>=128:
            raise _error(source_folder)
        receipt = file_journal.safe_path(self.work,'group-'+key+'.json')
        staged = file_journal.safe_path(self.work,'stage-'+key)
        marker = file_journal.safe_path(self.work,'verified-'+key+'.json')
        identity = sha256(delivery.encode(manifest)).hexdigest()
        if file_journal.exists(receipt):
            inner = self.receipt(manifest,key)
            self.check_layer(inner,layer)
            self.check_result(manifest,inner,key,record_paths=False)
            if not source:
                self.release(manifest,source_folder,key,inner['layer'])
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
                inner = self.extract(manifest,source_folder,staged,layer,allow_standard=source)
            delivery.write_new(marker,dict(identity=identity,inner=inner))
        self.check_layer(inner,layer)
        self.check()
        if inner['layer']==1:
            restored = self.named_group(manifest,key,1,create=True)
            self.publish_group(inner,staged,restored)
        else:
            for index,child in enumerate(inner['children'],1):
                if child['tool']!=manifest['tool']:
                    raise _error(staged)
                self.group(child,staged,f'{key}-{index}',inner['layer']-1)
        if not source and self.retain_layer(inner['layer']):
            self.named_group(manifest,key,inner['layer'],retained=True,create=True)
        delivery.write_new(receipt,dict(identity=identity,inner=inner))
        if not source:
            self.release(manifest,source_folder,key,inner['layer'])
        self.remove_stage(inner,staged)
        marker.unlink(missing_ok=True)

    def check_result(self,manifest,inner,key,*,record_paths=True):
        if inner['layer']==1:
            folder = self.named_group(manifest,key,1)
            _check_files(folder,self.targets(inner),self.cancel)
            if record_paths:
                if folder in self.published_paths:
                    raise _error(folder)
                self.published_paths.add(folder)
        else:
            for index,child in enumerate(inner['children'],1):
                child_key = f'{key}-{index}'
                node = self.receipt(child,child_key)
                if node['layer']!=inner['layer']-1:
                    raise _error(self.work)
                self.check_result(child,node,child_key,record_paths=record_paths)
                if self.retain_layer(node['layer']):
                    kept = self.named_group(child,child_key,node['layer'],retained=True)
                    _check_files(kept,{row['shown']:row for row in child['volumes']},self.cancel)
                    if record_paths:
                        if kept in self.published_paths:
                            raise _error(kept)
                        self.published_paths.add(kept)

    def release(self,manifest,folder,key,layer):
        if file_journal.exists(folder) and (not folder.is_dir() or folder.is_symlink() or folder.is_junction()):
            raise _error(folder)
        retain = self.retain_layer(layer)
        if retain:
            kept = self.named_group(manifest,key,layer,retained=True)
            kept.mkdir(parents=True,exist_ok=True)
            if kept.is_junction() or kept.is_symlink():
                raise _error(kept)
        for row in manifest['volumes']:
            path = file_journal.safe_path(folder,row['shown'])
            if retain:
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

    def extract(self,manifest,source_folder,staged,layer,*,allow_standard=False,external=None):
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
                if allow_standard and row['shown'].casefold()!=row['canonical'].casefold():
                    standard = file_journal.safe_path(source_folder,row['canonical'])
                    if file_journal.exists(standard):
                        if file_journal.exists(source):
                            raise ValueError(tr('同一分卷同时存在两种名称：{0} 和 {1}',source,standard))
                        source = standard
                if not file_journal.exists(source):
                    raise ValueError(tr('缺少交付分卷：{0}',source))
                target = volumes/row['canonical']
                self.copy(source,target,row)
                if row['shown']==manifest['entry']:
                    entry = target
            _format_matches(manifest,entry)
            password = self.password(tool,entry,manifest,layer)
            self.check()
            sizes = _members(tool,entry,password,self.force,embedded=external is None)
            check_write(payload,sum(sizes.values())+(sizes[delivery.MEMBER] if external is None else 0),tr('解包并恢复'))
            if external is None:
                engine._run_proc(tool.extract_command(entry,metadata,password,delivery.MEMBER),self.force)
                inner_path = metadata/delivery.MEMBER
                if {item.name for item in metadata.iterdir()}!={delivery.MEMBER}:
                    raise _error(metadata)
                inner = delivery.read(inner_path,delivery.validate_members)
                expected = self.expected(inner,inner_path)
            else:
                inner = external
                rows = inner['files'] if layer==1 else [row for child in inner['children'] for row in child['volumes']]
                expected = {row['name'] if layer==1 else row['shown']:row for row in rows}
            self.check_layer(inner,layer)
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


class TaskDeliveryUnpack(Unpack):
    def unpack_selected(self,root,path,manifest,selection,keeps,originals):
        fingerprint = sha256(delivery.encode({k:v for k,v in manifest.items() if k!='passwords'})).hexdigest()
        started = datetime.now()
        existing = {}
        for item in root.iterdir():
            match = re.fullmatch(r'unpack-\d{4}-\d{2}-\d{2}_\d{6}-.+-([a-f0-9]{16}-\d+)',item.name)
            if match:
                existing.setdefault(match[1],[]).append(item.name)
        for index in selection:
            self.check()
            group = manifest['groups'][index]
            self.manifest_path = path
            legacy = f'unpack-{fingerprint[:24]}-{index+1}'
            token = f'{fingerprint[:16]}-{index+1}'
            candidates = existing.get(token,[])
            if file_journal.exists(root/legacy):
                candidates = [legacy,*candidates]
            if len(candidates)>1:
                raise _error(root)
            label = file_journal.recovery_label(group['tree']['archive']['stem'])
            workspace = candidates[0] if candidates else f'unpack-{started:%Y-%m-%d_%H%M%S}-{label}-{token}'
            self.work = file_journal.safe_path(root,workspace)
            if self.work.is_junction():
                raise _error(self.work)
            self.work.mkdir(exist_ok=True)
            try:
                with file_journal.legacy_workspace_lock(self.work):
                    checkpoint = file_journal.safe_path(self.work,'unpack.json')
                    keep = keeps.get(str(index),[])
                    original = originals.get(str(index),self.original)
                    if not _valid_retention(keep,group['layer_count']) or type(original) is not bool:
                        raise _error(checkpoint)
                    if file_journal.exists(checkpoint):
                        info = delivery.read(checkpoint,lambda data:data)
                        if (not isinstance(info,dict) or info.get('key')!=fingerprint
                                or info.get('index')!=index or type(info.get('original')) is not bool
                                or not _valid_retention(info.get('keep'),group['layer_count'])):
                            raise _error(checkpoint)
                    else:
                        if any(item.name!=file_journal.LOCK for item in self.work.iterdir()):
                            raise _error(self.work)
                        info = dict(key=fingerprint,index=index,keep=keep,original=original)
                        delivery.write_new(checkpoint,info)
                    self.options = info
                    folder = path.parent
                    if group['folder']:
                        folder = file_journal.safe_path(folder,group['folder'])
                    if folder.is_symlink() or folder.is_junction():
                        raise _error(folder)
                    complete = self.expand(group['tree'],folder,'1',fingerprint,index,source=True)
                if complete:
                    self.clear_workspace()
                    self.stats['groups'] += 1
                    if self.progress:
                        self.progress(0,100,self.stats['groups'],self.stats['groups_total'],
                                      tr('解包并恢复'))
                else:
                    self.stats['recovery_path'] = str(self.work)
            except (file_journal.Cancelled,engine.ForceCancelled):
                self.stats['recovery_path'] = str(self.work)
                raise
            except (OSError,ValueError,subprocess.CalledProcessError) as error:
                # Tool exceptions include command-line arguments.
                detail = (tr('解包工具运行失败（退出码 {0}）',error.returncode)
                          if isinstance(error,subprocess.CalledProcessError) else str(error))
                self.stats['error_detail'] = detail
                self.stats['recovery_path'] = str(self.work)
                print(detail)

    def clear_workspace(self):
        artifacts = tuple(self.work.iterdir())
        for item in artifacts:
            if item.name in ('unpack.json',file_journal.LOCK):
                file_journal.identity(item)
            elif not re.fullmatch(r'(?:stage-[0-9-]+|(?:extract|publish)-[a-z0-9_]{8})',item.name):
                raise _error(item)
        for item in artifacts:
            if item.name not in ('unpack.json',file_journal.LOCK):
                _remove_scratch(item)
        (self.work/file_journal.LOCK).unlink(missing_ok=True)
        (self.work/'unpack.json').unlink()
        self.work.rmdir()

    def expand(self,node,folder,key,fingerprint,index,*,source=False):
        self.check()
        archive,inner = node['archive'],node['members']
        layer = inner['layer']
        staged = file_journal.safe_path(self.work,'stage-'+key)
        if not file_journal.exists(staged):
            self.extract(archive,folder,staged,layer,allow_standard=source,external=inner)
        elif not staged.is_dir() or staged.is_junction():
            raise _error(staged)
        expected = ([(row['name'],row) for row in inner['files']] if layer==1 else
                    [(row['shown'],row) for child in inner['children'] for row in child['volumes']])
        _check_files(staged,dict(expected),self.cancel)
        complete = True
        if layer==1:
            for row in inner['files']:
                if row['auxiliary']:
                    continue
                self.check()
                filename = row.get('original',row['name']) if self.options['original'] else row['name']
                target = '/'.join(filter(None,(row['directory'],filename)))
                if not self.merge_file(staged/row['name'],target,row):
                    complete = False
        else:
            for number,child in enumerate(node['children'],1):
                if not self.expand(child,staged,f'{key}-{number}',fingerprint,index):
                    complete = False
        if not source and self.retain_layer(layer):
            base = tr('保留的压缩包')
            prefix = f"{base}/{tr('第 {0} 层',layer)}/{fingerprint[:8]}-{index+1}-{key}"
            for row in archive['volumes']:
                self.check()
                if not self.merge_file(folder/row['shown'],prefix+'/'+row['shown'],row):
                    complete = False
        return complete

    def merge_file(self,source,relative,row):
        try:
            target = file_journal.safe_path(self.destination,relative)
            if file_journal.exists(target):
                _match(target,row,self.cancel)
                return True
            target.parent.mkdir(parents=True,exist_ok=True)
            check_write(target.parent,row['size'],tr('解包并恢复'))
            with TemporaryDirectory(prefix='publish-',dir=self.work) as scratch:
                temp = Path(scratch)/'file'
                self.copy(source,temp,row)
                target = file_journal.safe_path(self.destination,relative)
                file_journal.rename_new(temp,target)
            self.stats['files_moved'] += 1
            return True
        except (OSError,ValueError):
            message = tr('文件未还原，目标位置有冲突或不可写：{0}',self.destination/relative)
            self.stats['error_detail'] = message
            print(message)
            return False


def run(config,stats,progress=None,cancel=None,force=None,prompt=None):
    paths = config['unpack_manifests']
    reviewed = [(path,delivery.read(Path(path),delivery.validate_delivery)) for path in paths]
    choices = config.get('unpack_task_choices',{})
    total = sum(len(choices.get(str(path),range(len(manifest['groups']))))
                if manifest.get('kind')=='sortzip-task' else 1 for path,manifest in reviewed)
    stats.update(groups=0,groups_total=total,files_moved=0,unpack_task=True)
    task = Unpack(config,progress,cancel,force,prompt,stats)
    batch = TaskDeliveryUnpack(config,progress,cancel,force,prompt,stats)
    try:
        task.destination.mkdir(parents=True,exist_ok=True)
        with file_journal.task_lock(task.destination):
            for path,manifest in reviewed:
                if manifest.get('kind')!='sortzip-task':
                    task.open_delivery(path)
                    continue
                fingerprint = sha256(delivery.encode(manifest)).hexdigest()
                if str(path) in batch.manifest_keys and batch.manifest_keys[str(path)]!=fingerprint:
                    raise ValueError(tr('解包清单已更改，请重新选择：{0}',path))
                selected = choices.get(str(path),range(len(manifest['groups'])))
                if any(type(index) is not int or not 0<=index<len(manifest['groups']) for index in selected):
                    raise _error(path)
                if not selected:
                    continue
                from sortzip_core.delivery_passwords import unlock
                try:
                    batch.shared_passwords = unlock(manifest,config.get('unpack_delivery_phrases',{}).get(str(path),''))
                except ValueError as error:
                    stats['error_detail'] = str(error)
                    print(error)
                    continue
                root = file_journal.ensure_recovery_directory(batch.destination)
                batch.unpack_selected(root,Path(path).resolve(),manifest,dict.fromkeys(selected),
                                      config.get('unpack_task_retention',{}).get(str(path),{}),
                                      config.get('unpack_task_original',{}).get(str(path),{}))
            task.cleanup_completed()
            if stats['groups']!=total:
                return 'partial'
            file_journal.remove_empty_recovery(task.destination)
            return 'completed'
    except (file_journal.Cancelled,engine.ForceCancelled):
        return engine._cancel_status(force)
    except subprocess.CalledProcessError as error:
        raise ValueError(tr('解包工具运行失败（退出码 {0}）',error.returncode)) from error
    finally:
        task.passwords.clear()
        batch.passwords.clear()
        batch.shared_passwords = {}
