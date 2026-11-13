import contextlib
import csv
import io
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sortzip_core import engine
from sortzip_core.archive_tools import check_tool,detect_tool,entry_name,shown_name
from sortzip_core.nested_resume import load_resume
from sortzip_core.nesting import Layer


class ArchiveToolTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root/'source 文件'
        self.source.mkdir()
        self.dest = self.root/'dest 中文'
        self.payloads = {}
        for index in range(1,4):
            name = f'第 {index} 个.bin'
            self.payloads[name] = random.Random(index).randbytes(3100)
            (self.source/name).write_bytes(self.payloads[name])
        self.config = dict(src=str(self.source),dest=str(self.dest),custom_names={'.bin':'材料'},
                           output_list=True,keep_files=True,compression_layers=[Layer()])

    def test_bandizip_preflight_uses_sibling_console_without_help_flag(self):
        launcher = self.root/'Bandizip.exe'
        launcher.touch()
        with patch.dict(os.environ,{'PATH':str(self.root),'ProgramFiles':'',
                                    'ProgramFiles(x86)':'','ProgramW6432':''}), \
                patch('sortzip_core.archive_tools.subprocess.run') as run:
            self.assertIsNone(detect_tool('bandizip'))
            with self.assertRaisesRegex(ValueError,'bz.exe'):
                check_tool('bandizip',{'bandizip':str(launcher)})
            run.assert_not_called()
            console = self.root/'bz.exe'
            console.touch()
            run.return_value = subprocess.CompletedProcess([],0,b'Bandizip console tool\nbz <command>',b'')
            self.assertEqual(check_tool('bandizip',{'bandizip':str(launcher)}),str(console.resolve()))
            self.assertEqual(run.call_args.args[0],[str(console.resolve())])

    def test_unavailable_tool_stops_before_moving_sources(self):
        config = dict(self.config,archive_tool='7zip',keep_files=False,
                      archive_tool_paths={'7zip':str(self.root/'missing.exe')})
        with self.assertRaisesRegex(ValueError,'找不到 7-Zip'):
            engine.main_from_config(config)
        self.assertFalse(self.dest.exists())
        config['compression_layers'] = [Layer(password='测试密码abcdef')]
        with self.assertRaisesRegex(ValueError,'ASCII'):
            engine.main_from_config(config)
        self.assertFalse(self.dest.exists())
        self.assertEqual(len(list(self.source.iterdir())),3)
        config['compression_layers'] = [Layer(),Layer(archive_format='lzh')]
        with self.assertRaisesRegex(ValueError,'第 2 层.*LZH'):
            engine.main_from_config(config)
        self.assertFalse(self.dest.exists())

    def test_volume_entry_and_display_name_do_not_accept_missing_or_mixed_volumes(self):
        settings = Layer(archive_format='rar',enable_volume=True)
        self.assertEqual(entry_name('winrar','a',settings,{'a.part01.rar','a.part02.rar'}),'a.part01.rar')
        self.assertEqual(shown_name('a.part01.rar','a','rar','.png'),'a.part01.png')
        for names in ({'a.part01.rar','a.part03.rar'},{'a.rar','a.part1.rar'},{'a.part1.rar','a.part01.rar'}):
            with self.assertRaises(ValueError):
                entry_name('winrar','a',settings,names)
        self.assertEqual(entry_name('7zip','a',Layer(enable_volume=True),{'a.zip.001'}),'a.zip.001')

    def test_tool_failure_and_logs_hide_console_password(self):
        secret = ':test-"sensitive"'
        cmd = ['7z.exe','a','-p'+secret,'out.7z']
        self.assertNotIn(secret,engine._command_text(cmd))
        process = subprocess.CompletedProcess(cmd,2,secret,secret)
        with patch.object(engine.subprocess,'Popen') as popen:
            popen.return_value.returncode = process.returncode
            popen.return_value.communicate.return_value = (process.stdout,process.stderr)
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                engine._run_proc(cmd)
            self.assertEqual(popen.call_args.kwargs['stdin'],subprocess.DEVNULL)
            self.assertEqual(popen.call_args.kwargs['creationflags'],getattr(subprocess,'CREATE_NO_WINDOW',0))
        for text in (str(caught.exception),caught.exception.stdout,caught.exception.stderr):
            self.assertNotIn(secret,text)

    def test_checkpoint_rejects_conflicting_backend_and_legacy_defaults_to_bandizip(self):
        with contextlib.redirect_stdout(io.StringIO()):
            from sortzip_core.nested_execution import execute_plan
            from sortzip_core.nesting import Unit, MaterializedUnit, plan_layers
            path = self.source/next(iter(self.payloads))
            execute_plan(plan_layers([Unit('a','materials',('a',))],[Layer()]),
                         {'a':MaterializedUnit((str(path),),True)},self.dest,cancel_check=lambda:True)
        checkpoint = engine._checkpoint_path(self.dest)
        state = json.loads(checkpoint.read_text(encoding='utf-8'))
        state['archive_tool'] = '../../unexpected.exe'
        checkpoint.write_text(json.dumps(state),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'压缩工具'):
            load_resume(checkpoint,source_roots=[self.root])
        state['schema'] = 2
        checkpoint.write_text(json.dumps(state),encoding='utf-8')
        self.assertEqual(load_resume(checkpoint,source_roots=[self.root])[2]['archive_tool'],'bandizip')

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in real command-line tools')
    def test_real_7zip_zip_and_7z_volumes(self):
        tool = os.environ.get('SORTZIP_TEST_7ZIP') or shutil.which('7z.exe')
        if not tool:
            self.skipTest('7z.exe unavailable')
        self.verify_real_workflow('7zip',tool,'zip','7z')

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in real command-line tools')
    def test_real_winrar_rar5_volumes(self):
        tool = os.environ.get('SORTZIP_TEST_WINRAR') or shutil.which('Rar.exe')
        if not tool:
            self.skipTest('Rar.exe unavailable')
        self.verify_real_workflow('winrar',tool,'rar','rar')

    def verify_real_workflow(self,tool,exe,inner,outer):
        secret = 'Test !@%& pass-123' if tool=='7zip' else '验收 !@%& pass-123'
        config = dict(self.config,archive_tool=tool,archive_tool_paths={tool:exe},compression_layers=[
            Layer(1,inner,True,'1KB',password=secret,verify=True),
            Layer(3,outer,True,'3KB',suffix='.png',password=secret,verify=True)])
        stopped = False
        def progress(start,end,done,total,message):
            nonlocal stopped
            if start==40 and done==1:
                stopped = True
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            status = engine.main_from_config(config,on_progress=progress,cancel_check=lambda:stopped)
        self.assertEqual(status,'safe_cancelled',log.getvalue())
        self.assertNotIn(secret,log.getvalue())
        checkpoint = engine._checkpoint_path(self.dest)
        before = json.loads(checkpoint.read_text(encoding='utf-8'))
        self.assertEqual(before['archive_tool'],tool)
        self.assertNotIn(exe,checkpoint.read_text(encoding='utf-8'))
        self.assertFalse((self.dest/'材料/List.txt').exists())
        params = dict(resume_from=str(checkpoint),archive_tool='bandizip',archive_tool_paths={tool:exe},
                      layer_passwords={0:secret,1:secret})
        code = ('import json,sys; from sortzip_core.engine import main_from_config; '
                'c=json.load(sys.stdin); c["layer_passwords"]={int(k):v for k,v in c["layer_passwords"].items()}; '
                'raise SystemExit(main_from_config(c) != "completed")')
        proc = subprocess.run([sys.executable,'-X','utf8','-c',code],input=json.dumps(params).encode(),
                              capture_output=True,timeout=45)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        self.assertNotIn(secret.encode(),proc.stdout+proc.stderr)
        after = json.loads(checkpoint.read_text(encoding='utf-8'))
        for identity,record in before['completed'].items():
            self.assertEqual(record,after['completed'][identity])
        final = after['completed'][after['final_ids'][0]]
        with (self.dest/'材料/List.txt').open(encoding='utf-8',newline='') as stream:
            rows = list(csv.DictReader(stream,delimiter='\t'))
        self.assertEqual(len(rows),3)
        self.assertTrue(all(row['所属压缩包名']==final['folder']+'/'+final['entry'] for row in rows))
        delivery = self.root/'delivery'
        shutil.copytree(self.dest/final['folder'],delivery)
        subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',
            str(delivery/'Restore-Archive-Names/Restore-Names.ps1')],check=True,capture_output=True,timeout=30)
        outer_entry = delivery/rows[0]['标准入口卷名']
        if tool=='winrar':
            self.assertEqual(outer_entry.read_bytes()[:8],b'Rar!\x1a\x07\x01\x00')
        unpacked = self.root/'unpacked'
        self.extract(tool,exe,outer_entry,unpacked,secret)
        for identity in final['inputs']:
            record = after['completed'][identity]
            self.assertEqual({p['shown'] for p in record['files']},
                             {p.name for p in unpacked.iterdir()} & {p['shown'] for p in record['files']})
            self.extract(tool,exe,unpacked/record['entry'],self.root/'files',secret)
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'files').iterdir()},self.payloads)
        second_volume = self.dest/final['folder']/final['files'][1]['shown']
        second_volume.unlink()
        with self.assertRaises(ValueError):
            load_resume(checkpoint,passwords={0:secret,1:secret})

        # Start a real tool process, then request force cancellation at the
        # first poll. The interrupted unit must never become resumable output.
        real_popen = subprocess.Popen
        children = []
        def capture_child(*args,**kwargs):
            process = real_popen(*args,**kwargs)
            if args[0][1]=='a':
                children.append(process)
            return process
        forced = self.root/'forced'
        with patch.object(engine.subprocess,'Popen',side_effect=capture_child),contextlib.redirect_stdout(io.StringIO()):
            status = engine.main_from_config(dict(config,dest=str(forced)),force_cancel_check=lambda:bool(children))
        self.assertEqual(status,'force_cancelled')
        self.assertEqual(len(children),1)
        self.assertIsNotNone(children[0].poll())
        self.assertNotEqual(children[0].returncode,0)
        forced_checkpoint = engine._checkpoint_path(forced)
        self.assertFalse(json.loads(forced_checkpoint.read_text(encoding='utf-8'))['completed'])
        with contextlib.redirect_stdout(io.StringIO()):
            status = engine.main_from_config(dict(params,resume_from=str(forced_checkpoint)))
        self.assertEqual(status,'completed')

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1','opt-in 7-Zip')
    def test_7zip_unicode_long_list_single_archive(self):
        executable = os.environ.get('SORTZIP_TEST_7ZIP') or shutil.which('7z.exe')
        if not executable:
            self.skipTest('7z.exe unavailable')
        payloads = dict(self.payloads)
        for i in range(600):
            name = f'长清单 {i:04d} '+('x'*24)+'.bin'
            payloads[name] = str(i).encode()
            (self.source/name).write_bytes(payloads[name])
        secret = '密码 !@%& abc123'
        config = dict(self.config,archive_tool='7zip',archive_tool_paths={'7zip':executable},
                      compression_layers=[Layer(1000,'7z',password=secret,verify=True)])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(engine.main_from_config(config),'completed')
        state = json.loads(engine._checkpoint_path(self.dest).read_text(encoding='utf-8'))
        record = state['completed'][state['final_ids'][0]]
        self.assertTrue(record['entry'].endswith('.7z'))
        out = self.root/'files'
        self.extract('7zip',executable,self.dest/record['folder']/record['entry'],out,secret)
        self.assertEqual({p.name:p.read_bytes() for p in out.iterdir()},payloads)

    def extract(self,tool,exe,entry,out,secret):
        out.mkdir(exist_ok=True)
        options = ['-cfg-',str(entry),str(out)+'\\'] if tool=='winrar' else [str(entry),'-o'+str(out)]
        proc = subprocess.run([exe,'x','-y','-p'+secret,*options],stdin=subprocess.DEVNULL,
                              capture_output=True,timeout=20)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
