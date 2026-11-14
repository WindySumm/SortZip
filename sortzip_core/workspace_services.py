"""Cancelable planning on one bounded Qt pool; stale revisions never replace drafts."""
from copy import deepcopy
from threading import Event
from PySide6.QtCore import QObject,QRunnable,QThreadPool,Signal,Slot
from sortzip_core.workspace_state import PreviewResult
from sortzip_core.workspace_planning import build_workspace_preview
from sortzip_core.i18n import tr


class _JobSignals(QObject):
    done = Signal(object,object)


class _Job(QRunnable):
    def __init__(self,config,revision,builder,strict):
        super().__init__()
        self.config = deepcopy({key:value for key,value in config.items() if key!='_preview_cache'})
        if '_preview_cache' in config:
            self.config['_preview_cache'] = config['_preview_cache']
        self.revision,self.builder,self.strict = revision,builder,strict
        self.stop = Event()
        self.signals = _JobSignals()

    def run(self):
        try:
            result = self.builder(self.config,self.revision,self.stop.is_set,self.strict)
        except Exception as error:
            # No configuration repr: a strict execution snapshot contains passwords.
            result = PreviewResult(self.revision,file_error=type(error).__name__+tr(': 后台预检失败'))
        self.signals.done.emit(self,result)


class BackgroundPlanner(QObject):
    ready = Signal(object)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.jobs = set()
        self.pending = None

    def submit(self,config,revision,strict=False,builder=build_workspace_preview):
        self.cancel()
        if self.jobs:
            self.pending = (config,revision,strict,builder)
            return
        self._start(config,revision,strict,builder)

    def _start(self,config,revision,strict,builder):
        job = _Job(config,revision,builder,strict)
        self.jobs.add(job)
        job.signals.done.connect(self._deliver)
        self.pool.start(job)

    @Slot(object,object)
    def _deliver(self,job,result):
        self.jobs.discard(job)
        if self.pending is not None:
            pending,self.pending = self.pending,None
            self._start(*pending)
        elif not job.stop.is_set():
            self.ready.emit(result)
        elif not self.jobs:
            self.ready.emit(None)

    def cancel(self):
        self.pending = None
        for job in tuple(self.jobs):
            job.stop.set()

    def busy(self):
        return bool(self.jobs) or self.pool.activeThreadCount()>0
