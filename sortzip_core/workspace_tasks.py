"""One task lifecycle for the workbench; snapshots outlive editable controls."""
from copy import deepcopy
from pathlib import Path
from PySide6.QtCore import QObject,Signal,Slot,QTimer
from sortzip_core.tasks import create_worker_thread
from sortzip_core.workspace_services import BackgroundPlanner
from sortzip_core.workspace_planning import inspect_resume,ResumeInspection
from sortzip_core.i18n import tr


class TaskController(QObject):
    active = Signal(bool)
    log = Signal(str)
    progress = Signal(int,int,str)
    failed = Signal(str)
    finished = Signal(object,str,str)
    idle = Signal()
    cancel_waiting = Signal()
    prepared = Signal(object)

    def __init__(self,parent=None,factory=None):
        super().__init__(parent)
        self.factory = factory or create_worker_thread
        self.thread = self.worker = None
        self._preparing = False
        self._cancel_requested = False
        self._pending = None
        self._confirm = None
        self._resume_callback = None
        self._secrets = []
        self.destination = ''
        self.planner = BackgroundPlanner(self)
        self.planner.ready.connect(self._prepared)

    def busy(self):
        return self._preparing or self.thread is not None or self.worker is not None

    def execute(self,config,revision,confirm):
        if self.busy():
            self.log.emit(tr('已有任务，已阻止重复启动。\n'))
            return False
        self._preparing,self._cancel_requested = True,False
        self._confirm,self._resume_callback = confirm,None
        self.active.emit(True)
        self.progress.emit(0,100,tr('正在只读预检'))
        self.planner.submit(dict(config,_check_archive_executable=True),revision,strict=True)
        return True

    def resume(self,path,callback,*,tool_paths=None):
        if self.busy():
            self.log.emit(tr('已有任务，暂不能恢复其他任务。\n'))
            return False
        self._preparing,self._cancel_requested = True,False
        self._resume_callback = callback
        self.active.emit(True)
        self.progress.emit(0,100,tr('正在校验断点'))
        self.planner.submit(dict(resume_from=path,archive_tool_paths=dict(tool_paths or {}),
                                 _check_archive_executable=True),0,builder=inspect_resume)
        return True

    @Slot(object)
    def _prepared(self,result):
        if not self._preparing:
            return
        if self._cancel_requested or result is None:
            self._release()
            return
        if isinstance(result,ResumeInspection):
            if result.error:
                self.failed.emit(tr('断点无效：')+result.error)
                self._release()
                return
            try:
                config = self._resume_callback(result)
            except (ValueError,OSError,TypeError,KeyError):
                self.failed.emit(tr('无法读取恢复参数；未启动文件操作。'))
                self._release()
                return
        else:
            error = result.file_error or result.archive_error
            if error or result.prepared is None:
                self.failed.emit(error or tr('预检未完成'))
                self._release()
                return
            self.prepared.emit(result)
            try:
                config = result.prepared if self._confirm(result) else None
            except Exception:
                self.failed.emit(tr('配置确认未完成；未启动文件操作。'))
                self._release()
                return
        if config is None or self._cancel_requested:
            self._release()
            return
        self._preparing = False
        self.launch(config,already_active=True)

    def launch(self,config,already_active=False):
        if self.busy():
            return False
        config = deepcopy(config)
        self._pending = None
        self.destination = config.get('dest','')
        if not self.destination and config.get('resume_from'):
            self.destination = str(Path(config['resume_from']).resolve().parent)
        self._secrets = [str(value) for value in [config.get('password',''),*config.get('layer_passwords',{}).values(),
            *(layer.password for layer in config.get('compression_layers',()))] if value]
        if not already_active:
            self.active.emit(True)
        try:
            self.thread,self.worker = self.factory(config)
            self.thread.setParent(self)
            self.worker.finished.connect(self._worker_finished)
            self.thread.finished.connect(self._thread_finished)
            self.worker.log.connect(self._log)
            self.worker.error.connect(lambda message:self._log(tr('[错误] ')+message+'\n'))
            self.worker.progress.connect(self.progress)
            self.worker.cancel_waiting.connect(self.cancel_waiting)
            self.thread.start()
            return True
        except Exception:
            self.thread = self.worker = None
            self.failed.emit(tr('无法创建后台任务，已恢复界面，请检查运行环境。'))
            self._release()
            return False

    @Slot(str)
    def _log(self,message):
        for secret in sorted(self._secrets,key=len,reverse=True):
            message = message.replace(secret,'********')
        self.log.emit(message)

    def cancel(self,force=False):
        if self._preparing:
            self._cancel_requested = True
            self.planner.cancel()
            self.log.emit(tr('已取消只读预检，未执行文件操作。\n'))
        elif self.worker is not None and self._pending is None and self.thread.isRunning():
            self.worker.force_cancel() if force else self.worker.cancel()
            self.log.emit(tr('已请求强制取消。\n') if force else tr('已排队安全取消，等待当前阶段完成并落盘。\n'))

    @Slot()
    def _worker_finished(self):
        self._pending = (dict(self.worker.stats),self.worker.status,self.destination)
        status = self.worker.status
        if status=='completed':
            self.progress.emit(100,100,tr('完成'))
        else:
            self.log.emit({'error':tr('任务失败'),'partial':tr('部分分组跳过'),'safe_cancelled':tr('已安全中断'),'force_cancelled':tr('已强制中断')}.get(status,tr('任务停止'))+'\n')

    @Slot()
    def _thread_finished(self):
        # finished is emitted before native thread-local/deferred QObject cleanup.
        # Keep both Python wrappers until wait(0) confirms that cleanup completed.
        QTimer.singleShot(0,self._finalize_thread)

    @Slot()
    def _finalize_thread(self):
        if self.thread is None:
            return
        if not self.thread.wait(0):
            QTimer.singleShot(10,self._finalize_thread)
            return
        pending = self._pending
        thread = self.thread
        self.thread = self.worker = None
        thread.deleteLater()
        self._pending = None
        self._release()
        if pending is not None:
            self.finished.emit(*pending)

    def _release(self):
        self._preparing = False
        self._secrets = []
        self.active.emit(False)
        self.idle.emit()
