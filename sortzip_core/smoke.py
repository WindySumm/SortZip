"""Isolated source/frozen startup verification; never touches application credentials."""
import csv
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class _MemoryCredentials:
    def __init__(self):
        self.password = ''

    def read(self):
        return self.password

    def write(self, password):
        self.password = password

    def delete(self):
        self.password = ''


def _archive_roundtrip(root, selected):
    from sortzip_core import engine
    from sortzip_core.archive_tools import ArchiveTool, resolve_tool
    from sortzip_core.nested_execution import execute_plan
    from sortzip_core.nesting import Layer, MaterializedUnit, Unit, plan_layers

    output = root/'legacy-output'
    source = output/'材料'
    source.mkdir(parents=True)
    payloads = {f'材料 {index}.bin':os.urandom(8192) for index in (1,2)}
    inputs = {}
    for name,payload in payloads.items():
        file = source/name
        file.write_bytes(payload)
        inputs[name] = MaterializedUnit((str(file),),True)
    password = 'SortZip smoke !@% 123'
    archive_format = 'rar' if selected=='winrar' else '7z'
    layers = [Layer(1,archive_format,True,'2KB',password=password,verify=True),
              Layer(2,archive_format,True,'3KB',suffix='.data',password=password,verify=True)]
    plan = plan_layers([Unit(name,'材料',(name,)) for name in inputs],layers,allow_rar=selected=='winrar')
    executable = resolve_tool(selected)
    stopped = False

    def pause(*_):
        nonlocal stopped
        stopped = True

    result = execute_plan(plan,inputs,output,archive_tool=ArchiveTool(selected,executable),
                          output_list=True,on_progress=pause,cancel_check=lambda:stopped)
    if result.status!='safe_cancelled' or len(result.records)!=1:
        raise RuntimeError('旧断点生成或取消失败')
    checkpoint = engine._checkpoint_path(output)
    before = json.loads(checkpoint.read_bytes())
    params = dict(resume_from=str(checkpoint),layer_passwords={0:password,1:password})
    if before['schema']!=3 or engine.main_from_config(params)!='completed':
        raise RuntimeError('旧断点恢复失败')
    after = json.loads(checkpoint.read_bytes())
    if any(after['completed'][key]!=record for key,record in before['completed'].items()):
        raise RuntimeError('已完成的旧归档未复用')
    outer = after['completed'][after['final_ids'][0]]
    with (source/'List.txt').open(encoding='utf-8',newline='') as stream:
        rows = list(csv.DictReader(stream,delimiter='\t'))
    if len(rows)!=len(payloads) or any(row['所属压缩包名']!=outer['folder']+'/'+outer['entry'] or
                                      not (output/row['所属压缩包名']).is_file() for row in rows):
        raise RuntimeError('清单没有引用实际入口卷')
    volumes = root/'canonical'
    shutil.copytree(output/outer['folder'],volumes)
    powershell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    restore = subprocess.run([str(powershell),'-NoProfile','-ExecutionPolicy','Bypass','-File',
                              str(volumes/'Restore-Archive-Names'/'Restore-Names.ps1')],
                             stdin=subprocess.DEVNULL,capture_output=True,timeout=30,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if restore.returncode:
        raise RuntimeError('后缀还原脚本执行失败')
    entry = volumes/rows[0]['标准入口卷名']
    unfolded = root/'unfolded'
    recovered = root/'recovered'
    for directory in (unfolded,recovered):
        directory.mkdir()

    def extract(archive, destination):
        if selected=='winrar':
            args = ['-cfg-', '-p'+password, str(archive), str(destination)+os.sep]
        elif selected=='bandizip':
            args = ['-p:'+password, '-o:'+str(destination), str(archive)]
        else:
            args = ['-p'+password, '-o'+str(destination), str(archive)]
        process = subprocess.run([executable,'x','-y',*args],stdin=subprocess.DEVNULL,
                                 capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if process.returncode:
            raise RuntimeError(f'{selected} 解包失败（退出码 {process.returncode}）：{archive.name}')

    extract(entry,unfolded)
    for key in outer['inputs']:
        record = after['completed'][key]
        if len(record['files'])<2:
            raise RuntimeError('内层未生成分卷')
        extract(unfolded/record['entry'],recovered)
    if len(outer['files'])<2 or {path.name:path.read_bytes() for path in recovered.iterdir()}!=payloads:
        raise RuntimeError('外层分卷或逐层解包内容不一致')
    return {'schema':before['schema'],'outer_volumes':len(outer['files']),'files_matched':len(payloads)}


def run_smoke_test(report_path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QSettings
    from PySide6.QtTest import QTest
    from shiboken6 import isValid
    import time
    from PySide6.QtWidgets import QApplication
    from sortzip_core.app import MainWindow
    from sortzip_core.i18n import LANGUAGE_KEY,configure_language,current_language,tr
    from sortzip_core import __version__
    from sortzip_core.planning import build_execution_plan
    from sortzip_core.widgets import resource_path

    application = QApplication.instance() or QApplication(['SortZip'])
    report = dict(status='error', version=__version__)
    try:
        with tempfile.TemporaryDirectory(prefix='sortzip-smoke-') as directory:
            root = Path(directory)
            settings = QSettings(str(root/'settings.ini'), QSettings.Format.IniFormat)
            settings.setValue('double_compress',True)
            settings.setValue('group_size',3)
            settings.sync()
            from sortzip_core.workspace_state import load_workspace
            migrated = load_workspace(settings)
            if migrated.invalid_migration or len(migrated.layers)!=2 or migrated.layers[0].data['group']!=3:
                raise RuntimeError('旧偏好迁移失败')
            settings.clear()
            settings.sync()
            report['legacy_preferences'] = 'ok'
            requested_language = os.environ.get('SORTZIP_SMOKE_LANGUAGE','zh')
            settings.setValue(LANGUAGE_KEY,requested_language)
            configure_language(settings,application)
            report['language'] = current_language()
            window = MainWindow(settings=settings, credential_store=_MemoryCredentials(), check_updates=False)
            try:
                if window.run_btn.text()!=tr('开始执行') or (requested_language=='en' and window.run_btn.text()!='Start task'):
                    raise RuntimeError('Could not load the requested interface language')
                if window.pages.count() != 4 or window.windowIcon().isNull():
                    raise RuntimeError('窗口页面或图标加载失败')
                if not Path(resource_path('assets/sortzip.ico')).is_file():
                    raise RuntimeError('打包图标资源缺失')
                from sortzip_core import restore_bundle
                if not Path(restore_bundle.__file__).with_name('restore_names.ps1').is_file():
                    raise RuntimeError('还原脚本资源缺失')
                if len(window.state.layers)!=1 or hasattr(window,'stack'):
                    raise RuntimeError('正式工作台未正确初始化')
                source = root/'src'
                from sortzip_core.workbench_prototype import WorkbenchPrototype
                prototype = WorkbenchPrototype()
                try:
                    prototype.navigate(prototype.state.layers[-1].key)
                    if len(prototype.state.layers)!=2 or prototype.tree.topLevelItemCount()!=2:
                        raise RuntimeError('工作台原型初始化失败')
                finally:
                    prototype.close()
                source.mkdir()
                (source/'a.txt').write_text('smoke', encoding='utf-8')
                window.source.setText(str(source))
                window.destination.setText(str(root/'out'))
                window._insert_mapping('.txt','doc')
                window.refresh()
                deadline = time.monotonic()+5
                while (window.state.preview is None or window.state.preview.revision!=window.state.revision) and time.monotonic()<deadline:
                    application.processEvents()
                    QTest.qWait(5)
                if window.filename_table.rowCount()!=1:
                    raise RuntimeError('后台真实文件预览未完成')
                plan = build_execution_plan(dict(src=str(source), dest=str(root/'out'),
                    custom_names={'.txt': 'doc'}, group_size=1, first_compress=False))
                if len(plan) != 1:
                    raise RuntimeError('执行计划加载失败')
                from sortzip_core.archive_tools import detect_tool, TOOL_NAMES
                tools = {identifier: bool(detect_tool(identifier)) for identifier in TOOL_NAMES}
                if 'SORTZIP_SMOKE_TOOL' in os.environ:
                    from sortzip_core.engine import main_from_config
                    from sortzip_core.nesting import Layer
                    selected = os.environ['SORTZIP_SMOKE_TOOL']
                    configured = sorted(identifier for identifier,found in tools.items() if found)
                    expected = sorted(os.environ.get('SORTZIP_SMOKE_DETECTED',selected).split(',')) if selected!='missing' else []
                    if configured!=expected:
                        raise RuntimeError('验收环境的可用压缩工具与预期不一致')
                    task = dict(src=str(source),dest=str(root/'smoke-archive'),custom_names={'.txt':'doc'},
                                keep_files=False,output_list=True)
                    from sortzip_core.constants import RESUME_FILE_NAME
                    from sortzip_core.file_journal import DATABASE
                    copied = dict(task,dest=str(root/'smoke-recovery'),first_compress=False,
                                  keep_files=True,archive_tool='bandizip')
                    interrupted = False
                    def stop_after_copy(start,end,done,total,message):
                        nonlocal interrupted
                        interrupted = start==0 and done==1
                    if main_from_config(copied,on_progress=stop_after_copy,cancel_check=lambda:interrupted)!='safe_cancelled':
                        raise RuntimeError('分类任务取消检查失败')
                    recovery = Path(copied['dest'])
                    if not (recovery/DATABASE).is_file() or main_from_config(dict(
                            resume_from=str(recovery/RESUME_FILE_NAME),approved_input_root=str(source)))!='completed':
                        raise RuntimeError('分类任务恢复检查失败')
                    if (recovery/'doc/a.txt').read_bytes()!=b'smoke' or not (recovery/'doc/List.txt').is_file():
                        raise RuntimeError('分类恢复内容或清单不匹配')
                    report['file_recovery_smoke'] = 'ok'
                    if selected=='missing':
                        task.update(archive_tool='bandizip',compression_layers=[Layer()])
                        try:
                            main_from_config(task)
                        except ValueError as error:
                            if str(error)!=tr('找不到 Bandizip 命令行程序 bz.exe。请在设置中选择 Bandizip 安装目录下的 bz.exe。'):
                                raise
                        else:
                            raise RuntimeError('缺少工具时任务未被阻止')
                        if Path(task['dest']).exists() or not (source/'a.txt').is_file():
                            raise RuntimeError('缺少工具时改动了文件')
                    else:
                        task.update(archive_tool=selected,compression_layers=[Layer(archive_format='rar' if selected=='winrar' else 'zip',verify=True)])
                        if main_from_config(task)!='completed' or not (Path(task['dest'])/'doc/List.txt').is_file():
                            raise RuntimeError('压缩工作流启动检查失败')
                        report['archive_roundtrip'] = _archive_roundtrip(root,selected)
                    report['archive_smoke'] = selected
                report.update(status='ok', pages=window.pages.count(),
                              initial_layers=len(window.state.layers), unified_ui=True,workbench=True,
                              restore_script_resource=True,
                              workbench_prototype=True,
                              formats=[window.layer_editor.format.itemText(i)
                                       for i in range(window.layer_editor.format.count())],
                              credentials='isolated-memory', updates='disabled', archive_tools=tools)
            finally:
                window.close()
                deadline = time.monotonic()+5
                while isValid(window) and (window.preview_service.busy() or window.controller.planner.busy()) and time.monotonic()<deadline:
                    application.processEvents()
                    QTest.qWait(5)
                if isValid(window):
                    window.close()
                application.processEvents()
    except Exception as error:
        report.update(status='error',error=str(error))
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['status'] == 'ok' else 1
