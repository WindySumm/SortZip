"""Logical-screen fitting with a scrollable workspace on low-height displays."""
from PySide6.QtCore import QTimer,Qt,QRect
from PySide6.QtWidgets import QScrollArea,QSizePolicy,QLayout


class WindowFitMixin:
    def install_screen_fit(self):
        self.workspace_scroll = QScrollArea(self)
        self.workspace_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.workspace_scroll.setWidgetResizable(True)
        self.workspace_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.workspace_scroll.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Ignored)
        self.layout().replaceWidget(self.outer,self.workspace_scroll)
        self.workspace_scroll.setWidget(self.outer)
        self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._fit_pending = False
        self._fit_screen = None
        self.fit_screen()

    def fit_screen(self,available=None):
        if not hasattr(self,'workspace_scroll') or getattr(self,'_closing',False):
            return
        screen = self.screen()
        area = available if available is not None else screen.availableGeometry()
        handle = self.windowHandle()
        margins = handle.frameMargins() if handle else None
        left,top,right,bottom = ((margins.left(),margins.top(),margins.right(),margins.bottom())
            if margins else (8,32,8,8))
        limit = area.adjusted(left,top,-right,-bottom)
        self.available_client_rect = limit
        self.compact_height = limit.height()<600
        self.outer.setMinimumHeight(420 if self.compact_height else 0)
        self.setMinimumSize(min(640,limit.width()),min(600,limit.height()))
        geometry = self.geometry()
        width,height = min(geometry.width(),limit.width()),min(geometry.height(),limit.height())
        x = max(limit.left(),min(geometry.x(),limit.right()-width+1))
        y = max(limit.top(),min(geometry.y(),limit.bottom()-height+1))
        self.setGeometry(QRect(x,y,width,height))
        if handle and not getattr(self,'_fit_bound',False):
            handle.screenChanged.connect(self._screen_changed)
            self._fit_bound = True
        if screen is not self._fit_screen:
            if self._fit_screen is not None:
                for name in ('availableGeometryChanged','logicalDotsPerInchChanged'):
                    try: getattr(self._fit_screen,name).disconnect(self._queue_fit)
                    except (RuntimeError,TypeError): pass
            self._fit_screen = screen
            screen.availableGeometryChanged.connect(self._queue_fit)
            screen.logicalDotsPerInchChanged.connect(self._queue_fit)

    def _screen_changed(self,*args):
        self._queue_fit()

    def _queue_fit(self,*args):
        if not self._fit_pending:
            self._fit_pending = True
            QTimer.singleShot(0,self._apply_queued_fit)

    def _apply_queued_fit(self):
        self._fit_pending = False
        self.fit_screen()

    def showEvent(self,event):
        super().showEvent(event)
        if hasattr(self,'workspace_scroll'): self._queue_fit()
