import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sortzip_core import engine
from sortzip_core.nested_execution import execute_plan
from sortzip_core.nesting import Layer, MaterializedUnit, Unit, plan_layers


class NestedExecutionTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.inputs = {}
        for i in range(1, 5):
            path = self.root/f'b{i}.bin'
            path.write_bytes(os.urandom(4096))
            self.inputs[f'b{i}'] = MaterializedUnit((str(path),), True)
        self.units = [Unit(key, 'doc', (key,)) for key in self.inputs]
        capture = contextlib.redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def fake_process(self, cmd, *args):
        if cmd[1]=='a':
            target = next(Path(p) for p in cmd[2:] if not p.startswith('-'))
            target.write_bytes(b'archive')
        return '', ''

    def test_publication_follows_persistence_and_no_inputs_are_deleted(self):
        plan = plan_layers(self.units, [Layer(1), Layer(2)])
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            result = execute_plan(plan, self.inputs, self.root/'out')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(result.records), 6)
        self.assertTrue(all(Path(unit.files[0]).is_file() for unit in self.inputs.values()))
        state = json.loads(engine._checkpoint_path(self.root/'out').read_text(encoding='utf-8'))
        self.assertEqual(state['completed'], result.records)

    def test_bandizip_always_suppresses_confirmation_for_every_archive(self):
        plan = plan_layers(self.units,[Layer(1,verify=True)])
        for auto in (False,True):
            with self.subTest(auto_close=auto),patch.object(engine,'_run_proc',side_effect=self.fake_process) as process:
                result = execute_plan(plan,self.inputs,self.root/str(auto),auto_close=auto)
                self.assertEqual(result.status,'completed')
                self.assertEqual([call.args[0][1] for call in process.call_args_list], ['a','t']*4)
                self.assertTrue(all('-y' in call.args[0] for call in process.call_args_list))

    def test_failed_checkpoint_does_not_publish_or_start_next_layer(self):
        original = engine._write_checkpoint
        def persist(root, state):
            if state['completed']:
                raise OSError('disk failure')
            original(root, state)
        with patch.object(engine, '_run_proc', side_effect=self.fake_process) as proc, \
                patch.object(engine, '_write_checkpoint', side_effect=persist):
            result = execute_plan(plan_layers(self.units, [Layer(1), Layer(2)]), self.inputs, self.root/'out')
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.records, {})
        self.assertEqual(proc.call_count, 1)

    def test_source_mutation_does_not_publish(self):
        def modified(cmd, *args):
            self.fake_process(cmd)
            Path(self.inputs['b1'].files[0]).write_bytes(b'changed')
            return '', ''
        with patch.object(engine, '_run_proc', side_effect=modified):
            result = execute_plan(plan_layers(self.units, [Layer(4)]), self.inputs, self.root/'out')
        self.assertEqual(result.status, 'error')
        self.assertFalse(result.records)

    def test_cancel_and_existing_checkpoint(self):
        plan = plan_layers(self.units, [Layer(1)])
        result = execute_plan(plan, self.inputs, self.root/'out', cancel_check=lambda:True)
        self.assertEqual(result.status, 'safe_cancelled')
        with self.assertRaises(ValueError):
            execute_plan(plan, self.inputs, self.root/'out')

    def test_checkpoint_retains_plan_but_never_password(self):
        plan = plan_layers(self.units, [Layer(4, password='private-password')])
        with patch.object(engine, '_run_proc', side_effect=self.fake_process):
            result = execute_plan(plan, self.inputs, self.root/'out')
        self.assertEqual(result.status, 'completed')
        text = engine._checkpoint_path(self.root/'out').read_text(encoding='utf-8')
        self.assertNotIn('private-password', text)
        state = json.loads(text)
        self.assertTrue(state['tasks'][0]['settings']['password_required'])
        self.assertEqual(len(state['sources']), 4)

    @unittest.skipUnless(os.environ.get('SORTZIP_INTEGRATION')=='1', 'opt-in real Bandizip')
    def test_real_three_layers_split_mixed_formats_and_roundtrip(self):
        from sortzip_core.nested_resume import resume_plan
        bz = shutil.which('bz')
        if not bz:
            self.skipTest('bz missing')
        plan = plan_layers(self.units, [Layer(1, '7z', True, '1k', verify=True),
            Layer(2, 'zip', True, '3k', verify=True), Layer(2, '7z', True, '5k', suffix='.png', verify=True)])
        stop = []
        def progress(done, total, identity):
            if done==5:
                stop.append(True)
        cancelled = execute_plan(plan, self.inputs, self.root/'out', bandizip=bz,
            cancel_check=lambda:bool(stop), on_progress=progress)
        self.assertEqual(cancelled.status,'safe_cancelled')
        self.assertEqual(len(cancelled.records),5)
        result = resume_plan(engine._checkpoint_path(self.root/'out'),
            source_roots=[self.root], bandizip=bz)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(result.records), 7)
        for task in plan.tasks:
            self.assertGreater(len(result.units[task.unit.unit_id].files), 1)
        recovered = {}
        def unpack(identity, available):
            if identity in self.inputs:
                recovered[identity] = available[0].read_bytes()
                return
            record = result.records[identity]
            folder = self.root/'restored'/identity.replace(':', '_')
            folder.mkdir(parents=True)
            for path, item in zip(available, record['files']):
                shutil.copyfile(path, folder/item['canonical'])
            entry = next(item['canonical'] for item in record['files'] if item['shown']==record['entry'])
            output = folder/'extracted'
            subprocess.run([bz, 'x', '-y', '-o:'+str(output), str(folder/entry)],
                check=True, capture_output=True, timeout=30)
            for child in record['inputs']:
                unpack(child, [output/Path(p).name for p in result.units[child].files])
        for identity in plan.final_ids:
            unpack(identity, [Path(p) for p in result.units[identity].files])
        self.assertEqual(recovered, {key:Path(unit.files[0]).read_bytes() for key,unit in self.inputs.items()})
