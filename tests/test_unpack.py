import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from sortzip_core import delivery_manifest as delivery, engine, file_journal, unpack
from sortzip_core.archive_tools import detect_tool
from sortzip_core.nesting import Layer


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root/'source'
        self.source.mkdir()
        self.files = {'中文 原名.txt':os.urandom(2800),'原文件.zip':b'archive as payload'}
        for name,data in self.files.items():
            (self.source/name).write_bytes(data)

    def config(self,tool,exe,layers,*,original=False,manual=False):
        return dict(src=str(self.source),dest=str(self.root/f'compress-{tool}'),
                    custom_names={'.txt':'doc','.zip':'doc'},keep_files=True,first_compress=True,
                    archive_tool=tool,archive_tool_paths={tool:exe},compression_layers=layers,
                    retain_layers=[],delivery_manifest=True,delivery_original_names=original,
                    restore_original_names=manual,naming_rules=[dict(enable=True,match_folder='doc',template='新的 = {n}{ext}')])

    def test_requires_flat_unique_names(self):
        base = dict(schema=1,layer=1,children=[],files=[dict(name='a.txt',size=0,sha256='0'*64,auxiliary=False)])
        delivery.validate_members(base)
        for bad in ('../escape.txt','x/y.txt','x\\y.txt','a:stream','NUL.txt',delivery.MEMBER):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                delivery.validate_members(dict(base,files=[dict(base['files'][0],name=bad)]))
        with self.assertRaises(ValueError):
            delivery.validate_members(dict(base,files=base['files']+[dict(base['files'][0],name='A.txt')]))

    def test_password_order_and_unknown_error(self):
        config = dict(dest=str(self.root/'receive'),unpack_passwords=['first','first','second'])
        task = unpack.Unpack(config,None,None,None,lambda *_:'extra',{})
        with patch.object(task,'try_password',side_effect=[False,False,True]) as attempt:
            self.assertEqual(task.password(None,None,{'encrypted':True,'entry':'name'},2),'extra')
            self.assertEqual([item.args[2] for item in attempt.call_args_list],['first','second','extra'])
            self.assertEqual(task.passwords[-1],'extra')
        ambiguous = subprocess.CalledProcessError(2,[],stderr='Data Error in encrypted file. Wrong password?')
        self.assertFalse(unpack._wrong_password('7zip',ambiguous))
        self.assertFalse(unpack._wrong_password('winrar',subprocess.CalledProcessError(3,[])))

    def test_seven_zip_tries_all_candidates_before_showing_ambiguous_warning(self):
        from sortzip_core.archive_tools import ArchiveTool
        entry = self.root/'sample.7z'
        manifest = {'encrypted':True,'entry':entry.name}
        error = subprocess.CalledProcessError(2,[],stderr='ERROR: Data Error in encrypted file. Wrong password? : a.txt')
        tool = ArchiveTool('7zip','7z')
        for results,expected_prompt in (([error,('', '')],False),([error,error,('', '')],True)):
            with self.subTest(prompt=expected_prompt):
                task = unpack.Unpack(dict(dest=str(self.root),unpack_passwords=['first','second']),
                                     None,None,None,lambda *_:'replacement',{})
                with patch.object(engine,'_run_proc',side_effect=results) as process,patch.object(
                        task,'prompt',wraps=task.prompt) as prompt:
                    self.assertEqual(task.password(tool,entry,manifest,2),'replacement' if expected_prompt else 'second')
                    self.assertEqual(process.call_count,3 if expected_prompt else 2)
                    if expected_prompt:
                        prompt.assert_called_once_with(2,entry.name,True)
                        self.assertEqual(task.passwords[-1],'replacement')
                    else:
                        prompt.assert_not_called()
        error.stderr += '\nERROR: Unexpected end of archive'
        with patch.object(engine,'_run_proc',side_effect=error) as process,self.assertRaises(ValueError):
            task.password(tool,entry,manifest,2)
        self.assertEqual(process.call_count,1)

    def test_seven_zip_distinguishes_supported_failure_messages(self):
        entry = self.root/'sample.7z'
        for message,expected in (
            ('ERROR: Wrong password : a.txt','wrong'),
            ('ERROR: CRC Failed in encrypted file. Wrong password? : a.txt','ambiguous'),
            (f'ERROR: {entry}\nCannot open encrypted archive. Wrong password?\n\nERRORS:\nHeaders Error','ambiguous'),
            ('ERROR: CRC Failed : a.txt',None),
            ('ERROR: Missing volume : sample.7z.002',None),
        ):
            with self.subTest(message=message):
                self.assertEqual(unpack._seven_zip_password_failure(
                    subprocess.CalledProcessError(2,[],stderr=message),entry),expected)

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real Bandizip')
    def test_same_named_deliveries_can_be_resumed_individually(self):
        exe = detect_tool('bandizip')
        if not exe:
            self.skipTest('Bandizip unavailable')
        paths = []
        for index in range(2):
            config = self.config('bandizip',exe,[Layer(2,'zip')],original=bool(index))
            config['dest'] = str(self.root/f'delivery-{index}')
            self.assertEqual(engine.main_from_config(config),'completed')
            paths.append(str(next(Path(config['dest']).rglob('*.sortzip.json'))))
        output = self.root/'received'
        options = dict(dest=str(output),unpack_manifests=paths,archive_tool_paths={'bandizip':exe})
        stats = {}
        def cancelled_after_first():
            return stats['groups']==1
        self.assertEqual(unpack.run(options,stats,cancel=cancelled_after_first),'safe_cancelled')
        self.assertEqual(unpack.run(dict(options,unpack_manifests=paths[:1]),{}),'completed')
        self.assertEqual(unpack.run(dict(options,unpack_manifests=paths[1:]),{}),'completed')
        result = {frozenset(path.name for path in folder.iterdir()) for folder in output.glob('*/group-1')}
        self.assertEqual(result,{frozenset(self.files),frozenset(('新的 = 1.txt','新的 = 2.zip'))})

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real compression tools')
    def test_encrypted_mixed_layers_resume_and_volume_names(self):
        tools = [('bandizip',detect_tool('bandizip'),('zip','7z','zip')),
                 ('7zip',detect_tool('7zip',os.environ.get('SORTZIP_TEST_7ZIP','')),('zip','7z','7z')),
                 ('winrar',detect_tool('winrar',os.environ.get('SORTZIP_TEST_WINRAR','')),('rar','rar','rar'))]
        for tool,exe,formats in tools:
            if not exe:
                continue
            with self.subTest(tool=tool):
                shared = 'outer 1234'
                inner = 'inner 1234'
                layers = [Layer(2,formats[0],password=inner),Layer(1,formats[1],password=inner),
                          Layer(1,formats[2],True,'2k',suffix='.data',password=shared)]
                config = self.config(tool,exe,layers,original=True,manual=True)
                self.assertEqual(engine.main_from_config(config),'completed')
                manifests = list(Path(config['dest']).rglob('*.sortzip.json'))
                self.assertEqual(len(manifests),1)
                manifest = delivery.read(manifests[0],delivery.validate_archive)
                self.assertGreater(len(manifest['volumes']),1)
                for secret in (inner,shared,*self.files):
                    self.assertNotIn(secret,manifests[0].read_text(encoding='utf-8'))
                originals = {row['shown']:(manifests[0].parent/row['shown']).read_bytes() for row in manifest['volumes']}
                receiver = dict(dest=str(self.root/f'receive-{tool}'),unpack_manifests=list(map(str,manifests)),
                                archive_tool_paths={tool:exe},unpack_keep=True,unpack_passwords=[shared])
                def cancelled_after_stage():
                    return bool(list(Path(receiver['dest']).glob('.sortzip-unpack-*/verified-1.json')))
                self.assertEqual(unpack.run(receiver,{},cancel=cancelled_after_stage),'safe_cancelled')
                self.assertFalse(list(Path(receiver['dest']).glob('1-*')))
                receiver.update(unpack_passwords=[shared,inner],unpack_keep=False)
                code = ('import json,sys; from sortzip_core.unpack import run; '
                        'sys.exit(run(json.loads(sys.argv[1]),{})!="completed")')
                proc = subprocess.run([sys.executable,'-c',code,json.dumps(receiver)],capture_output=True,timeout=40)
                self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
                output = next(Path(receiver['dest']).glob('1-*'))
                self.assertTrue((output/'_archives').is_dir())
                group = output/'group-1-1-1'
                self.assertEqual({path.name:path.read_bytes() for path in group.iterdir()},self.files)
                self.assertEqual({name:(manifests[0].parent/name).read_bytes() for name in originals},originals)
                with self.assertRaisesRegex(ValueError,'解包目标已存在'):
                    unpack.run(receiver,{})

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real Bandizip')
    def test_default_keeps_renamed_files_missing_volume_and_bad_hash(self):
        exe = detect_tool('bandizip')
        if not exe:
            self.skipTest('Bandizip unavailable')
        config = self.config('bandizip',exe,[Layer(2,'zip',True,'2k')])
        self.assertEqual(engine.main_from_config(config),'completed')
        path = next(Path(config['dest']).rglob('*.sortzip.json'))
        manifest = delivery.read(path,delivery.validate_archive)
        folder = self.root/'received'
        options = dict(dest=str(folder),unpack_manifests=[str(path)],archive_tool_paths={'bandizip':exe})
        volume = path.parent/manifest['volumes'][0]['shown']
        content = volume.read_bytes()
        volume.unlink()
        with self.assertRaisesRegex(ValueError,'缺少交付分卷'):
            unpack.run(options,{})
        volume.write_bytes(b'!'*len(content))
        with self.assertRaisesRegex(ValueError,'解包文件校验失败'):
            unpack.run(options,{})
        volume.write_bytes(content)
        from types import SimpleNamespace
        with patch('sortzip_core.disk_space.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError,'可用'):
                unpack.run(options,{})
        self.assertFalse(list(folder.glob('1-2-*')))
        self.assertEqual(unpack.run(options,{}),'completed')
        result = next(folder.glob('1-2-*'))
        names = {item.name for item in (result/'group-1').iterdir()}
        self.assertEqual(names,{'新的 = 1.txt','新的 = 2.zip'})
        self.assertFalse((result/'_archives').exists())

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','real Bandizip')
    def test_unsafe_members_rejected_before_any_extraction(self):
        exe = detect_tool('bandizip')
        if not exe:
            self.skipTest('Bandizip unavailable')
        for index in range(2):
            archive = self.root/f'unsafe-{index}.zip'
            with zipfile.ZipFile(archive,'w') as output:
                output.writestr(delivery.MEMBER,'{}')
                if index==0:
                    output.writestr('../outside.txt','bad')
                else:
                    info = zipfile.ZipInfo('malicious')
                    info.create_system = 3
                    info.external_attr = 0xa1ff<<16
                    output.writestr(info,'../outside.txt')
            manifest = dict(schema=1,tool='bandizip',format='zip',stem=archive.stem,entry=archive.name,
                            encrypted=False,suffix=None,volumes=[delivery.describe(archive)])
            path = archive.with_suffix(delivery.EXTENSION)
            delivery.write_new(path,manifest)
            calls = []
            original = engine._run_proc
            def recording(command,*args,**kwargs):
                calls.append(command[1])
                return original(command,*args,**kwargs)
            with patch.object(engine,'_run_proc',side_effect=recording),self.assertRaises(ValueError):
                unpack.run(dict(dest=str(self.root/'receive'),unpack_manifests=[str(path)],
                                archive_tool_paths={'bandizip':exe}),{})
            self.assertNotIn('e',calls)
