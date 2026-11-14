"""Background execution and thread creation; UI presentation stays in app/widgets."""
import sys
from threading import Event

from PySide6.QtCore import QObject, QThread, Signal, Slot
from sortzip_core.engine import main_from_config, redact


class LogStream:
    def __init__(self, signal):
        self.signal = signal

    def write(self, text):
        if text:
            self.signal.emit(text)

    def flush(self):
        pass


class Worker(QObject):
    log = Signal(str)
    error = Signal(str)
    progress = Signal(int, int, str)
    cancel_waiting = Signal()
    finished = Signal()
    unpack_password_requested = Signal(object)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._cancelled = False
        self._force_cancelled = False
        self.status = "completed"
        self.stats = {"files_moved": 0, "files_renamed": 0, "groups": 0}

    def cancel(self):
        self._cancelled = True
        self.cancel_waiting.emit()

    def force_cancel(self):
        self._force_cancelled = True
        self._cancelled = True

    def _cancel_check(self):
        return self._cancelled

    def _force_cancel_check(self):
        return self._force_cancelled

    def _unpack_password(self,layer,entry,ambiguous=False):
        reply = [Event(),None,layer,entry,ambiguous]
        self.unpack_password_requested.emit(reply)
        while not reply[0].wait(.1):
            if self._cancelled:
                return None
        return reply[1]

    @Slot()
    def run(self):
        old_stdout = sys.stdout
        sys.stdout = LogStream(self.log)
        try:
            progress = lambda start,end,cur,total,msg:self._report_progress(start,end,cur,total,msg)
            if 'unpack_manifests' in self.config:
                from sortzip_core.unpack import run
                self.status = run(self.config,self.stats,progress,self._cancel_check,
                                  self._force_cancel_check,self._unpack_password)
                return
            self.status = main_from_config(
                self.config,
                on_progress=progress,
                cancel_check=self._cancel_check,
                force_cancel_check=self._force_cancel_check,
                stats=self.stats,
            )
        except Exception as e:
            self.status = "error"
            message = redact(e,self.config.get('password',''))
            for password in (self.config.get('layer_passwords') or {}).values():
                message = redact(message,password)
            self.stats.setdefault('error_detail',message)
            self.error.emit(message)
        finally:
            self.config.pop('unpack_passwords',None)
            sys.stdout = old_stdout
            self.finished.emit()

    def _report_progress(self, start_pct, end_pct, cur, total, msg):
        if total <= 0:
            pct = start_pct
        else:
            pct = start_pct + (end_pct - start_pct) * cur // total
        pct = min(99, pct)  # 100% is reserved for a successful finished result.
        self.progress.emit(pct, 100, msg)


def create_worker_thread(config):
    """Return an unstarted thread/worker so the caller can connect UI signals first."""
    thread = QThread()
    worker = Worker(config)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    # Keep the worker valid until the thread actually exits, including the short
    # interval between its finished signal and delivery of GUI close/cancel events.
    thread.finished.connect(worker.deleteLater)
    return thread, worker
