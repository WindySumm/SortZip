"""Repeatable preview benchmarks; all generated output stays in ignored storage."""
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sortzip_core.workspace_state import WorkspaceState
from sortzip_core.workspace_planning import build_workspace_preview
from sortzip_core.planning_context import PlanningContext


def positive(value):
    number = int(value)
    if number<1: raise argparse.ArgumentTypeError('must be positive')
    return number


def measure(config,revision,source,*,strict=False,reference=False):
    scans = 0
    original = Path.iterdir
    def counted(path):
        nonlocal scans
        if path==source: scans += 1
        return original(path)
    def context(cancelled,**kwargs):
        return PlanningContext(cancelled,cache_paths=False if reference else kwargs['cache_paths'])
    with patch.object(Path,'iterdir',counted),patch('sortzip_core.workspace_planning.PlanningContext',context):
        begin = time.perf_counter()
        result = build_workspace_preview(config,revision,strict=strict)
        elapsed = time.perf_counter()-begin
    if result is None or result.file_error or result.archive_error:
        raise RuntimeError('benchmark planning failed' if result is None else result.file_error or result.archive_error)
    return result,dict(seconds=elapsed,scans=scans,stages=result.timings)


def render_measure(state,repeats,root):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from sortzip_core.workbench import WorkbenchWindow
    from sortzip_core.smoke import _MemoryCredentials
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(dir=root) as temp:
        window = WorkbenchWindow(QSettings(str(Path(temp)/'settings.ini'),QSettings.Format.IniFormat),
            _MemoryCredentials(),check_updates=False)
        try:
            window.timer.stop()
            window.preview_service.cancel()
            window.state = state
            window._populate_controls()
            window.timer.stop()
            window._filename_render_key = window._tree_render_key = None
            begin = time.perf_counter()
            window._render_preview()
            initial = time.perf_counter()-begin
            samples = []
            for _ in range(repeats):
                begin = time.perf_counter()
                window._render_preview()
                samples.append(time.perf_counter()-begin)
            return dict(initial_seconds=initial,same_result_median=statistics.median(samples),samples=samples)
        finally:
            window.close()
            while window.preview_service.busy() or window.controller.planner.busy():
                app.processEvents();time.sleep(.005)
            app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--materials',type=Path,help='Existing generated benchmark directory containing count subdirectories')
    parser.add_argument('--counts',type=positive,nargs='+',default=[1000,10000])
    parser.add_argument('--repeats',type=positive,default=3)
    parser.add_argument('--group-size',type=positive,default=100)
    parser.add_argument('--report',type=Path,default=ROOT/'.build-validation'/'preview-optimized.json')
    options = parser.parse_args()
    root = options.materials.resolve() if options.materials else ROOT/'.build-validation'/'benchmarks'/uuid.uuid4().hex[:8]
    root.mkdir(parents=True,exist_ok=True)
    reports = []
    for count in options.counts:
        source = root/str(count)
        if not options.materials:
            source.mkdir()
            for index in range(count): (source/f'file{index:05}.txt').write_text(f'generated {index}')
        if not source.is_dir(): raise ValueError(f'Missing material folder: {source}')
        state = WorkspaceState(source=str(source),destination=str(root/f'out-{count}'),
            mappings=[('.txt','doc')],mapping_enabled=[True])
        state.layers[0].data['group'] = options.group_size
        config = state.to_config(False)
        first,_ = measure(config,0,source)  # Warm disk and interpreter before sampling.
        renamed = dict(config,_preview_cache=first,naming_rules=[dict(match_folder='doc',template='renamed_{n}{ext}')])
        samples = {key:[] for key in ('initial','cached_replan','strict','reference_initial','reference_replan')}
        for repeat in range(options.repeats):
            for mode in samples:
                strict = mode=='strict'
                cached = mode in ('cached_replan','strict','reference_replan')
                result,sample = measure(renamed if cached else config,repeat+1,source,
                    strict=strict,reference=mode.startswith('reference'))
                assert sample['scans']==(0 if cached and not strict else 1),(mode,sample)
                assert len(result.entries)==count
                samples[mode].append(sample)
        medians = {mode:statistics.median(sample['seconds'] for sample in values) for mode,values in samples.items()}
        state.preview = first
        display = render_measure(state,options.repeats,root)
        report = dict(files=count,medians=medians,samples=samples,display=display,
            cached_speedup=1-medians['cached_replan']/medians['reference_replan'])
        reports.append(report)
        print(json.dumps(dict(files=count,medians=medians,display=display),ensure_ascii=True),flush=True)
    options.report.parent.mkdir(parents=True,exist_ok=True)
    large = next((row for row in reports if row['files']==10000),None)
    checks = {}
    if large:
        checks['cached_improves_50_percent'] = large['cached_speedup']>=.5
        checks['initial_no_regression'] = large['medians']['initial']<=large['medians']['reference_initial']*1.1
        baseline = ROOT/'.build-validation'/'preview-benchmark.json'
        if baseline.is_file() and options.group_size==100:
            old = next(row for row in json.loads(baseline.read_text())['results'] if row['files']==10000)
            checks['cached_vs_prior_baseline'] = large['medians']['cached_replan']<=old['cached_replan_seconds']*.5
            checks['strict_vs_prior_baseline'] = large['medians']['strict']<=old['strict_seconds']*1.1
    passed = all(checks.values())
    options.report.write_text(json.dumps(dict(status='ok' if passed else 'failed',materials=str(root),
        repeats=options.repeats,group_size=options.group_size,checks=checks,
        reference='same planner with full per-target resolution; no preview path cache',results=reports),indent=2),encoding='utf-8')
    if not passed: raise RuntimeError('benchmark acceptance failed: '+str(checks))
    print(options.report,flush=True)


if __name__=='__main__': main()
