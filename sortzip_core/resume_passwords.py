"""Verification for new checkpoints and read-only tests of old archives."""
import hashlib
import hmac
from contextlib import nullcontext
from pathlib import Path
import secrets
import subprocess
import tempfile

from sortzip_core.archive_tools import entry_name
from sortzip_core.i18n import tr


def _derive(password,salt):
    return hashlib.scrypt(password.encode('utf-8'),salt=salt,n=2**17,r=8,p=1,maxmem=160*1024**2)


def make_checks(layers):
    checks = {}
    for index,layer in enumerate(layers):
        if layer.password:
            salt = secrets.token_bytes(16)
            checks[str(index)] = dict(salt=salt.hex(),digest=_derive(layer.password,salt).hex())
    return checks


def validate_checks(checks,encrypted):
    if not isinstance(checks,dict) or checks.keys()!={str(level) for level in encrypted}:
        raise ValueError(tr('断点缺少分层密码校验数据'))
    for check in checks.values():
        if (not isinstance(check,dict) or set(check)!={'salt','digest'} or any(
                not isinstance(check[key],str) or len(check[key])!=length or
                any(char not in '0123456789abcdef' for char in check[key])
                for key,length in (('salt',32),('digest',128)))):
            raise ValueError(tr('断点密码校验数据无效'))


def legacy_sample(root,config,groups):
    from sortzip_core.engine import _first_artifacts, _safe_relative
    fmt = config.get('archive_format','zip')
    for group in groups:
        folder = _safe_relative(root,group['folder'])
        base = group['base_name']
        if group['state']=='first_done' or (group['state']=='final_done' and not config.get('double_compress',True)):
            stem = base + config.get('first_suffix','-First')
            disguise = config.get('first_suffix_replace') if not config.get('enable_volume',True) else None
            paths = _first_artifacts(folder,base,config.get('first_suffix','-First'),disguise,fmt)
            names = {path:(stem+'.'+fmt if disguise and path.name==stem+disguise else path.name) for path in paths}
        elif group['state']=='final_done':
            stem = base
            path = folder/(base+config.get('archive_suffix','.zipp'))
            names = {path:stem+'.'+fmt} if path.is_file() else {}
        else:
            continue
        for name in (stem+'.'+fmt,stem+'.'+fmt+'.001'):
            if name in names.values():
                return names,name
    return None


def _test_sample(tool,paths,entry,password,cancel_check,force_cancel_check):
    from sortzip_core import engine
    renamed = any(path.name!=name for path,name in paths.items())
    with (tempfile.TemporaryDirectory(prefix='sortzip-verify-') if renamed else nullcontext()) as temporary:
        if renamed:
            folder = Path(temporary)
            for path,name in paths.items():
                with path.open('rb') as incoming,(folder/name).open('xb') as output:
                    while block := incoming.read(1024*1024):
                        if cancel_check and cancel_check():
                            return False
                        output.write(block)
            archive = folder/entry
        else:
            archive = next(path for path,name in paths.items() if name==entry)
        engine._run_proc(tool.test_command(archive,password),force_cancel_check)
    return not (cancel_check and cancel_check())


def verify_legacy(root,config,groups,executable,password,cancel_check,force_cancel_check):
    from sortzip_core.archive_tools import ArchiveTool
    if not config.get('password'):
        return True
    if not password:
        raise ValueError(tr('请输入原任务密码'))
    sample = legacy_sample(root,config,groups)
    if sample is None:
        return True
    paths,entry = sample
    try:
        return _test_sample(ArchiveTool('bandizip',executable),paths,entry,password,cancel_check,force_cancel_check)
    except subprocess.CalledProcessError as error:
        raise ValueError(tr('Bandizip 原归档校验失败，请检查密码和归档：{0}', next(iter(paths)))) from error


def verify(root,tool,plan,state,passwords,cancel_check=None,force_cancel_check=None):
    done = state['completed']
    pending = {task.layer_index:task.settings for task in plan.tasks
               if task.unit.unit_id not in done and task.settings.password}
    samples = {}
    for task in plan.tasks[:len(done)]:
        samples.setdefault(task.layer_index,(task,done[task.unit.unit_id]))
    for layer,settings in pending.items():
        if cancel_check and cancel_check():
            return False
        password = passwords[layer]
        if state['schema']==4:
            check = state['password_checks'][str(layer)]
            if not hmac.compare_digest(_derive(password,bytes.fromhex(check['salt'])),bytes.fromhex(check['digest'])):
                raise ValueError(tr('第 {0} 层密码与原任务不一致', layer+1))
        elif layer in samples:
            task,record = samples[layer]
            folder = root/record['folder']
            paths = {folder/item['shown']:item['canonical'] for item in record['files']}
            entry = entry_name(tool.identifier,task.archive_stem,settings,set(paths.values()))
            try:
                if not _test_sample(tool,paths,entry,password,cancel_check,force_cancel_check):
                    return False
            except subprocess.CalledProcessError as error:
                raise ValueError(tr('{0} 第 {1} 层原归档校验失败，请检查密码和归档：{2}', tool.name, layer+1, folder)) from error
    return not (cancel_check and cancel_check())
