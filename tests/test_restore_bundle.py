import hashlib
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Event, Thread
import unittest

from sortzip_core.restore_bundle import (
    CHINESE_CATEGORY_LAUNCHER, CHINESE_PAYLOAD_LAUNCHER, category_launcher, write_bundle, write_category_bundle,
)


class RestoreBundleTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.a,self.b = self.root/'a.txt',self.root/'b.txt'
        self.a.write_text('first')
        self.b.write_text('second')
        self.kit = self.root/'还原名字 脚本'

    def run_script(self):
        return subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass',
            '-File',str(self.kit/'Restore-Names.ps1')],capture_output=True,timeout=30)

    def run_category(self):
        return subprocess.run(['cmd.exe','/d','/c','Restore-Names.cmd'],cwd=self.kit,input=b'\r\n',
                              capture_output=True,timeout=30)

    def category_payload(self):
        lines = (self.kit/'Restore-Names.cmd').read_bytes().splitlines()
        start = lines.index(b'::SORTZIP-SCRIPT')+2
        marker = lines.index(b'::SORTZIP-MANIFEST')
        return (base64.b64decode(b''.join(lines[start:marker])),
                base64.b64decode(b''.join(lines[marker+2:])))

    def test_portable_manifest_no_absolute_paths_and_no_overwrite(self):
        write_bundle(self.kit,[(self.a,'original.txt')])
        text = (self.kit/'Restore-Names.json').read_text(encoding='utf-8')
        self.assertNotIn(str(self.root),text)
        self.assertTrue((self.kit/'Restore-Names.cmd').is_file())
        write_bundle(self.kit,[(self.a,'original.txt')])
        (self.kit/'Restore-Names.ps1').write_text('keep me')
        with self.assertRaises(FileExistsError):
            write_bundle(self.kit,[(self.a,'original.txt')])
        self.assertEqual((self.kit/'Restore-Names.ps1').read_text(),'keep me')

    def test_invalid_targets_rejected(self):
        for target in ('../escape.txt','CON.txt','bad/name.txt'):
            with self.subTest(target=target),self.assertRaises(ValueError):
                write_bundle(self.kit,[(self.a,target)])
        with self.assertRaises(ValueError):
            write_bundle(self.kit,[(self.a,'same.txt'),(self.b,'SAME.txt')])
        self.assertFalse(self.kit.exists())

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_localized_cmds_launch_fixed_scripts_from_chinese_paths(self):
        for language in ('zh','en'):
            with self.subTest(language=language):
                category = self.root/f'视频 分类 {language}'
                category.mkdir()
                archive = category/'当前 名称.data'
                archive.write_bytes(b'volume')
                nested = category/'子目录 & 卷'
                nested.mkdir()
                piece = nested/'分卷 01.data'
                piece.write_bytes(b'nested volume')
                other = category/'另一 子目录'
                other.mkdir()
                other_piece = other/'分卷 02.data'
                other_piece.write_bytes(b'other volume')
                mappings = [(archive,'标准 名称.zip',None),(piece,'分卷 01.zip',None),
                            (other_piece,'分卷 02.zip',None)]
                write_category_bundle(category,mappings,language=language,adjacent=True)
                launcher = CHINESE_CATEGORY_LAUNCHER if language=='zh' else 'Restore-Names.cmd'
                self.assertEqual(list(category.glob('*.cmd')),[category/launcher])
                self.assertFalse((category/'Restore-Archive-Names').exists())
                result = subprocess.run(['cmd.exe','/d','/c',launcher],cwd=category,input=b'\r\n',
                                        capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual((category/'标准 名称.zip').read_bytes(),b'volume')
                self.assertEqual((nested/'分卷 01.zip').read_bytes(),b'nested volume')
                self.assertEqual((other/'分卷 02.zip').read_bytes(),b'other volume')
                self.assertFalse((category/'.sortzip-restore-names.lock').exists())
                member = category/'整理后.txt'
                member.write_bytes(b'payload')
                embedded = category/'解包 文件'
                write_bundle(embedded,[(member,'原始 文件.txt')],embedded=True,language=language)
                member.rename(embedded/member.name)
                launcher = CHINESE_PAYLOAD_LAUNCHER if language=='zh' else 'SortZip-Restore-Names.cmd'
                result = subprocess.run(['cmd.exe','/d','/c',launcher],cwd=embedded,input=b'\r\n',
                                        capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual((embedded/'原始 文件.txt').read_bytes(),b'payload')
                script = embedded/'SortZip-Restore-Names.ps1'
                command = ("[Threading.Thread]::CurrentThread.CurrentUICulture = "
                           "[Globalization.CultureInfo]::GetCultureInfo('zh-CN'); "
                           f"& '{script}'; exit $LASTEXITCODE")
                result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
                                         '-Command',command],capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertNotIn(b'Already restored',result.stdout)

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_category_restore_skips_and_later_recovers_missing_or_staged_files(self):
        category = self.root/'视频'
        sub = category/'课程'
        sub.mkdir(parents=True)
        self.kit = category/'Restore-Archive-Names'
        paths = [sub/f'卷-{index}.data' for index in range(6)]
        for index,path in enumerate(paths):
            path.write_bytes(bytes([index]))
        write_category_bundle(self.kit,[(path,path.with_suffix('.zip').name,None) for path in paths])
        for path in paths[1:]:
            path.unlink()
        result = self.run_category()
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        self.assertIn(b'Restored: 1.',result.stdout)
        self.assertIn(b'Missing: 5.',result.stdout)
        self.assertEqual(paths[0].with_suffix('.zip').read_bytes(),b'\0')
        paths[1].write_bytes(b'bad')
        paths[2].write_bytes(b'\x02')
        paths[2].with_suffix('.zip').write_bytes(b'occupied')
        _,manifest = self.category_payload()
        prefix = hashlib.sha256(manifest).hexdigest()[:24].upper()
        (sub/f'.sortzip-restore-{prefix}-3.tmp').write_bytes(b'\x03')
        result = self.run_category()
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        self.assertIn(b'Already restored: 1.',result.stdout)
        self.assertIn(b'Failed: 1.',result.stdout)
        self.assertIn(b'Occupied: 1.',result.stdout)
        self.assertEqual(paths[3].with_suffix('.zip').read_bytes(),b'\x03')
        self.assertEqual(paths[1].read_bytes(),b'bad')
        self.assertEqual(paths[2].with_suffix('.zip').read_bytes(),b'occupied')
        paths[1].write_bytes(b'\x01')
        self.assertEqual(self.run_category().returncode,2)
        self.assertEqual(paths[1].with_suffix('.zip').read_bytes(),b'\x01')

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_category_cmd_pauses_only_when_attention_needed(self):
        source = self.root/'volume.data'
        source.write_bytes(b'archive')
        write_category_bundle(self.root,[(source,'volume.zip',None)],adjacent=True)
        command = ['cmd.exe','/d','/c','Restore-Names.cmd']
        for _ in range(2):
            with subprocess.Popen(command,cwd=self.root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT) as proc:
                try:
                    self.assertEqual(proc.wait(timeout=20),0)
                    self.assertIn(b'Restored:',proc.stdout.read())
                finally:
                    if proc.poll() is None:
                        proc.kill()
        (self.root/'volume.zip').unlink()
        with subprocess.Popen(command,cwd=self.root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as proc:
            summary = Event()
            def watch_output():
                for line in proc.stdout:
                    if b'Missing: 1.' in line:
                        summary.set()
            reader = Thread(target=watch_output,daemon=True)
            reader.start()
            try:
                self.assertTrue(summary.wait(timeout=20))
                with self.assertRaises(subprocess.TimeoutExpired):
                    proc.wait(timeout=0.3)
                proc.stdin.write(b'\r\n')
                proc.stdin.flush()
                self.assertEqual(proc.wait(timeout=20),2)
            finally:
                if proc.poll() is None:
                    proc.kill()
                reader.join(timeout=20)

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_category_restore_refuses_paths_outside_the_category(self):
        category = self.root/'视频'
        sub = category/'课程'
        sub.mkdir(parents=True)
        volume = sub/'archive.data'
        volume.write_bytes(b'volume')
        self.kit = category/'Restore-Archive-Names'
        write_category_bundle(self.kit,[(volume,'archive.zip',None)])
        launcher = self.kit/'Restore-Names.cmd'
        original = launcher.read_bytes()
        script,manifest = self.category_payload()
        broken = original.splitlines()
        broken[broken.index(b'::SORTZIP-MANIFEST')+1] = b'0'*64
        launcher.write_bytes(b'\r\n'.join(broken)+b'\r\n')
        self.assertEqual(self.run_category().returncode,1)
        self.assertEqual(volume.read_bytes(),b'volume')
        invalid = json.loads(manifest)
        invalid['files'][0]['to'] = '../archive.zip'
        launcher.write_bytes(category_launcher(script,json.dumps(invalid).encode('utf-8')))
        self.assertEqual(self.run_category().returncode,1)
        self.assertEqual(volume.read_bytes(),b'volume')
        launcher.write_bytes(original)
        outside = self.root/'outside'
        sub.rename(outside)
        subprocess.run(['cmd','/c','mklink','/J',str(sub),str(outside)],check=True,capture_output=True)
        try:
            result = self.run_category()
            self.assertIn(b'Unsafe directory:',result.stdout)
            self.assertEqual((outside/volume.name).read_bytes(),b'volume')
            self.assertFalse((outside/'archive.zip').exists())
        finally:
            sub.rmdir()

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_category_restore_large_manifest_in_one_cmd(self):
        self.kit = self.root/'恢复 & 工具!'
        volumes = [self.root/f'档案 {i:03d}.data' for i in range(96)]
        for volume in volumes:
            volume.write_text('volume')
        mappings = [(volume,volume.with_suffix('.zip').name,None) for volume in volumes]
        write_category_bundle(self.kit,mappings)
        write_category_bundle(self.kit,mappings)
        launcher = self.kit/'Restore-Names.cmd'
        self.assertGreater(launcher.stat().st_size,8191)
        self.assertLess(max(map(len,launcher.read_bytes().splitlines())),8191)
        result = self.run_category()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertTrue(all(volume.with_suffix('.zip').is_file() for volume in volumes))
        self.assertEqual(list(self.kit.iterdir()),[launcher])

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_two_phase_swap_and_repeated_execution(self):
        write_bundle(self.kit,[(self.a,'b.txt'),(self.b,'a.txt')])
        result = self.run_script()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.a.read_text(),'second')
        self.assertEqual(self.b.read_text(),'first')
        self.assertEqual(self.run_script().returncode,0)

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_partial_staging_and_commit_can_resume(self):
        write_bundle(self.kit,[(self.a,'b.txt'),(self.b,'a.txt')])
        manifest = json.loads((self.kit/'Restore-Names.json').read_text(encoding='utf-8'))
        first = manifest['files'][0]
        self.a.rename(self.root/first['temp'])
        state = dict(schema=1,job=manifest['job'],manifest=hashlib.sha256(
            (self.kit/'Restore-Names.json').read_bytes()).hexdigest().upper(),phase='staging')
        (self.kit/'Restore-State.json').write_text(json.dumps(state),encoding='utf-8')
        result = self.run_script()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.a.read_text(),'second')
        self.assertEqual(self.b.read_text(),'first')

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_missing_or_conflicting_target_stops_before_any_rename(self):
        write_bundle(self.kit,[(self.a,'one.txt'),(self.b,'two.txt')])
        (self.root/'two.txt').write_text('unrelated')
        self.assertEqual(self.run_script().returncode,1)
        self.assertEqual(self.a.read_text(),'first')
        self.assertEqual(self.b.read_text(),'second')
        self.assertFalse((self.root/'one.txt').exists())
        (self.root/'two.txt').unlink()
        self.b.unlink()
        self.assertEqual(self.run_script().returncode,1)
        self.assertTrue(self.a.exists())
        self.assertFalse((self.root/'one.txt').exists())

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_partial_commit_resumes_and_manifest_changes_are_rejected(self):
        write_bundle(self.kit,[(self.a,'b.txt'),(self.b,'a.txt')])
        manifest_path = self.kit/'Restore-Names.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for record in manifest['files']:
            (self.root/record['from']).rename(self.root/record['temp'])
        first = manifest['files'][0]
        (self.root/first['temp']).rename(self.root/first['to'])
        state = dict(schema=1,job=manifest['job'],manifest=hashlib.sha256(
            manifest_path.read_bytes()).hexdigest().upper(),phase='committing')
        (self.kit/'Restore-State.json').write_text(json.dumps(state),encoding='utf-8')
        result = self.run_script()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.a.read_text(),'second')
        self.assertEqual(self.b.read_text(),'first')
        manifest['files'][0]['to']='other.txt'
        manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
        self.assertEqual(self.run_script().returncode,1)
        self.assertFalse((self.root/'other.txt').exists())

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_staging_conflict_reports_path_before_renaming_more_files(self):
        write_bundle(self.kit,[(self.a,'原来 A.txt'),(self.b,'原来 B.txt')])
        manifest_path = self.kit/'Restore-Names.json'
        manifest = json.loads(manifest_path.read_bytes())
        temporary = self.root/manifest['files'][0]['temp']
        self.a.rename(temporary)
        state = dict(schema=1,job=manifest['job'],manifest=hashlib.sha256(
            manifest_path.read_bytes()).hexdigest().upper(),phase='staging')
        (self.kit/'Restore-State.json').write_text(json.dumps(state),encoding='utf-8')
        conflict = self.root/'原来 B.txt'
        conflict.write_text('keep me')
        result = self.run_script()
        self.assertEqual(result.returncode,1)
        self.assertIn(b'Target already exists:',result.stdout)
        self.assertEqual(self.b.read_text(),'second')
        self.assertEqual(temporary.read_text(),'first')
        self.assertEqual(conflict.read_text(),'keep me')
        conflict.unlink()
        self.assertEqual(self.run_script().returncode,0)
        self.assertEqual(conflict.read_text(),'second')

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_locked_job_does_not_start_a_second_restore(self):
        import ctypes
        from ctypes import wintypes
        create = ctypes.WinDLL('kernel32',use_last_error=True).CreateFileW
        create.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,
                           wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = ctypes.WinDLL('kernel32',use_last_error=True).CloseHandle
        close.argtypes = [wintypes.HANDLE]
        write_bundle(self.kit,[(self.a,'original.txt')])
        handle = create(str(self.kit/'Restore-Names.lock'),0x40000000,0,None,4,0x80,None)
        self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
        try:
            result = self.run_script()
            self.assertEqual(result.returncode,1)
            self.assertIn(b'Could not lock this restore job',result.stdout)
            self.assertTrue(self.a.exists())
        finally:
            close(handle)
        self.assertEqual(self.run_script().returncode,0)

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1' and os.name=='nt','Windows script integration')
    def test_case_only_restore_and_unchanged_file(self):
        write_bundle(self.kit,[(self.a,'A.txt'),(self.b,'b.txt')])
        result = self.run_script()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual({path.name for path in self.root.glob('*.txt')},{'A.txt','b.txt'})
        self.assertEqual((self.root/'A.txt').read_text(),'first')
        self.assertEqual(self.b.read_text(),'second')
        self.assertEqual(self.run_script().returncode,0)
