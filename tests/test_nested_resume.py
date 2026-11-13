import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sortzip_core import engine
from sortzip_core.nested_execution import execute_plan
from sortzip_core.nested_resume import load_resume, resume_plan
from sortzip_core.nesting import Layer, MaterializedUnit, Unit, plan_layers


class NestedResumeTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.output = self.root/'out'
        self.inputs = {}
        for key in ('a','b'):
            path = self.root/(key+'.txt')
            path.write_text(key)
            self.inputs[key] = MaterializedUnit((str(path),), True)
        self.plan = plan_layers([Unit(key,'doc',(key,)) for key in self.inputs], [Layer(1),Layer(2)])
        capture = contextlib.redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__,None,None,None)

    def fake_process(self, cmd, *args):
        if cmd[1]=='a':
            path = next(Path(p) for p in cmd[2:] if not p.startswith('-'))
            path.write_bytes(b'archive')
        return '', ''

    def cancelled_checkpoint(self):
        stop = []
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            result = execute_plan(self.plan, self.inputs, self.output,
                cancel_check=lambda:bool(stop), on_progress=lambda *args:stop.append(True))
        self.assertEqual(result.status,'safe_cancelled')
        return engine._checkpoint_path(self.output)

    def test_resume_reuses_completed_and_is_repeatable(self):
        checkpoint = self.cancelled_checkpoint()
        with patch.object(engine, '_run_proc', side_effect=self.fake_process) as process:
            result = resume_plan(checkpoint, source_roots=[self.root])
            self.assertEqual(result.status,'completed')
            self.assertEqual(process.call_count,2)
            process.reset_mock()
            self.assertEqual(resume_plan(checkpoint,source_roots=[self.root]).status,'completed')
            process.assert_not_called()

    def test_external_source_requires_explicit_root(self):
        checkpoint = self.cancelled_checkpoint()
        with self.assertRaisesRegex(ValueError,'允许'):
            load_resume(checkpoint)

    def test_modified_pending_source_and_completed_artifact_rejected(self):
        checkpoint = self.cancelled_checkpoint()
        source = Path(self.inputs['b'].files[0])
        before = source.stat()
        source.write_text('changed')
        with self.assertRaises(ValueError):
            load_resume(checkpoint,source_roots=[self.root])
        source.write_text('b')
        import os
        os.utime(source,ns=(before.st_atime_ns,before.st_mtime_ns))
        data = json.loads(checkpoint.read_text(encoding='utf-8'))
        record = next(iter(data['completed'].values()))
        (self.output/record['folder']/record['entry']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            load_resume(checkpoint,source_roots=[self.root])

    def test_failed_attempt_is_preserved_and_retry_uses_new_directory(self):
        def failed(cmd,*args):
            self.fake_process(cmd)
            raise OSError('interrupted')
        with patch.object(engine,'_run_proc',side_effect=failed):
            result = execute_plan(self.plan,self.inputs,self.output)
        self.assertEqual(result.status,'error')
        leftover = next(self.output.rglob('*.zip'))
        before = leftover.read_bytes()
        with patch.object(engine,'_run_proc',side_effect=self.fake_process):
            result = resume_plan(engine._checkpoint_path(self.output),source_roots=[self.root])
        self.assertEqual(result.status,'completed')
        self.assertEqual(leftover.read_bytes(),before)
        self.assertTrue(any('.retry-' in r['folder'] for r in result.records.values()))

    def test_tampered_paths_graph_and_intermediate_suffix_rejected(self):
        checkpoint = self.cancelled_checkpoint()
        original = json.loads(checkpoint.read_text(encoding='utf-8'))
        for case in ('path','cycle','suffix','final','future_complete'):
            data = json.loads(json.dumps(original))
            if case=='path':
                next(iter(data['completed'].values()))['folder'] = '../outside'
            elif case=='cycle':
                data['tasks'][0]['inputs'] = [data['tasks'][0]['unit_id']]
            elif case=='suffix':
                data['tasks'][0]['settings']['suffix'] = '.png'
            elif case=='final':
                data['final_ids'] = ['unknown']
            else:
                data['completed'][data['tasks'][-1]['unit_id']] = {}
            checkpoint.write_text(json.dumps(data),encoding='utf-8')
            with self.subTest(case=case), self.assertRaises(ValueError):
                load_resume(checkpoint,source_roots=[self.root])

    def test_password_is_supplied_outside_checkpoint(self):
        plan = plan_layers([Unit('a','doc',('a',))],[Layer(password='password123')])
        with patch.object(engine,'_run_proc',side_effect=self.fake_process):
            execute_plan(plan,{'a':self.inputs['a']},self.output,cancel_check=lambda:True)
        checkpoint = engine._checkpoint_path(self.output)
        with self.assertRaisesRegex(ValueError,'密码'):
            load_resume(checkpoint,source_roots=[self.root])
        loaded,_,_ = load_resume(checkpoint,passwords={0:'password123'},source_roots=[self.root])
        self.assertEqual(loaded.tasks[0].settings.password,'password123')
