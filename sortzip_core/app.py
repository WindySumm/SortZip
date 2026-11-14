"""SortZip GUI entry and update notification; the workbench owns production UI."""
import sys
from PySide6.QtCore import QSettings,QThread,Signal
from PySide6.QtWidgets import QApplication
from sortzip_core.workbench import WorkbenchWindow as MainWindow
from sortzip_core.i18n import configure_language

class UpdateChecker(QThread):
    check_done = Signal(str)

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            import json
            import urllib.request
            url = "https://api.github.com/repos/WindySumm/SortZip/releases/latest"
            req = urllib.request.Request(url, headers={"User-Agent": "SortZip"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if not self.isInterruptionRequested():
                    self.check_done.emit(data.get("tag_name", ""))
        except Exception:
            if not self.isInterruptionRequested():
                self.check_done.emit("")


def _compare_versions(v1, v2):
    try:
        def parse(v):
            return tuple(int(x) for x in v.lstrip("vV").split("."))
        return parse(v1) > parse(v2)
    except Exception:
        return False



def main():
    if sys.argv[1:]==['--workbench-prototype']:
        from sortzip_core.workbench_prototype import main as prototype_main
        sys.exit(prototype_main())
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-test':
        from sortzip_core.smoke import run_smoke_test
        sys.exit(run_smoke_test(sys.argv[2]))
    app = QApplication(sys.argv)
    settings = QSettings('SortZip','SortZip')
    configure_language(settings, app)
    win = MainWindow(settings=settings)
    win.show()
    sys.exit(app.exec())
