import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from sortzip_core.restore_bundle import write_bundle


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
