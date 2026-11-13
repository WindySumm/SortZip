"""Preview-only optimizations must preserve planning and path safety contracts."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sortzip_core.planning import build_execution_plan
from sortzip_core.planning_context import PlanningContext,PlanningCancelled
from sortzip_core.nesting import Unit,Layer,plan_layers
from sortzip_core.workspace_state import WorkspaceState
from sortzip_core.workspace_planning import build_workspace_preview


class PreviewOptimizationTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'__pycache__'
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        source = self.root/'source'
        source.mkdir()
        for i in range(16): (source/f'file{i}.txt').write_text(str(i))
        self.state = WorkspaceState(source=str(source),destination=str(self.root/'output'),
            mappings=[('.txt','doc')],mapping_enabled=[True])

    def test_layer_only_change_reuses_names_and_does_not_mutate_cached_rows(self):
        config = self.state.to_config(False)
        first = build_workspace_preview(config,0)
        before = deepcopy(first.file_base)
        config.update(_preview_cache=first,group_size=3,
            compression_layers=(replace(config['compression_layers'][0],group_size=3,archive_format='7z',suffix='.png'),))
        with patch('sortzip_core.workspace_planning.build_execution_plan',side_effect=AssertionError('names recalculated')):
            second = build_workspace_preview(config,1)
        self.assertFalse(second.file_error or second.archive_error)
        self.assertEqual(first.file_base,before)
        self.assertEqual(second.file_plan[0]['group'],'1-3')
        self.assertTrue(second.file_plan[0]['archive'].endswith('.png'))
        self.assertNotIn('layer-',second.file_base[0]['archive'])

    def test_parent_resolution_is_request_local_and_leaf_links_are_followed(self):
        parent = self.root/'output'/'doc'
        original = os.lstat
        calls = []
        def checked(path,*args,**kwargs):
            calls.append(path)
            return original(path,*args,**kwargs)
        expected = parent.resolve()/'a.txt'
        with patch('os.lstat',checked):
            context = PlanningContext()
            self.assertEqual(context.target(parent/'a.txt'),expected)
            context.target(parent/'b.txt')
            self.assertEqual(calls.count(parent),1)
            PlanningContext().target(parent/'c.txt')
            self.assertEqual(calls.count(parent),2)
        leaf = self.root/'link.txt'
        try: leaf.symlink_to(self.root/'source'/'file0.txt')
        except OSError: pass  # Junction coverage below does not require symlink privilege.
        else: self.assertEqual(PlanningContext().target(leaf),leaf.resolve())

    def test_strict_context_never_reuses_parent_resolution(self):
        context = PlanningContext(cache_paths=False)
        original = Path.resolve
        with patch.object(Path,'resolve',autospec=True,side_effect=original) as resolve:
            context.target(self.root/'output'/'a.txt')
            context.target(self.root/'output'/'b.txt')
        self.assertEqual(resolve.call_count,2)
        self.assertFalse(context.directories)

    @unittest.skipUnless(os.name=='nt','Windows directory junction contract')
    def test_cached_names_recheck_new_output_junction_escape(self):
        first = build_workspace_preview(self.state.to_config(False),0)
        output = Path(self.state.destination)
        output.mkdir()
        outside = self.root/'outside'
        outside.mkdir()
        subprocess.run(['cmd','/c','mklink','/J',str(output/'doc'),str(outside)],
            check=True,capture_output=True)
        config = self.state.to_config(False)
        config['_preview_cache'] = first
        second = build_workspace_preview(config,1)
        self.assertIn('越界',second.file_error)
        strict = build_workspace_preview(config,2,strict=True)
        self.assertTrue(strict.file_error or strict.archive_error)

    def test_cancel_inside_file_and_archive_loops_is_not_an_error_result(self):
        config = self.state.to_config(False)
        inventory = build_workspace_preview(config,0).inventory
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            return calls>10
        with self.assertRaises(PlanningCancelled):
            build_execution_plan(config,inventory=inventory,context=PlanningContext(cancelled))
        calls = 0
        with self.assertRaises(PlanningCancelled):
            plan_layers([Unit(str(i),'doc',(str(i),)) for i in range(100)],
                (Layer(),),context=PlanningContext(cancelled))
        calls = 0
        cached = build_workspace_preview(config,0)
        def cancelled_in_plan():
            nonlocal calls
            calls += 1
            return calls>25
        config.update(_preview_cache=cached,naming_rules=[dict(match_folder='*',template='new_{n}{ext}')])
        self.assertIsNone(build_workspace_preview(config,1,cancelled_in_plan))
        self.assertEqual(calls,26)

    def test_strict_scan_detects_new_source_and_changed_metadata(self):
        first = build_workspace_preview(self.state.to_config(False),0)
        source = Path(self.state.source)
        (source/'new.txt').write_text('new')
        (source/'file0.txt').write_text('changed file content')
        config = self.state.to_config(False)
        config['_preview_cache'] = first
        strict = build_workspace_preview(config,1,strict=True)
        self.assertEqual(strict.total_files,17)
        row = next(row for row in strict.file_plan if row['source_rel']=='file0.txt')
        self.assertEqual(row['size'],len('changed file content'))
        self.assertFalse(strict.file_error or strict.archive_error)


if __name__=='__main__': unittest.main()
