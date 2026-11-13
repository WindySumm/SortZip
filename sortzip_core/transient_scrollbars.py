"""Thumb-only scroll overlays; native scroll models never reserve a gutter."""
from PySide6.QtCore import QObject,QEvent,QTimer,QPropertyAnimation,QEasingCurve,Qt
from PySide6.QtWidgets import QApplication,QAbstractScrollArea,QGraphicsOpacityEffect,QScrollBar,QStyle,QStyleOptionSlider
from PySide6.QtGui import QPainter,QColor


class OverlayScrollBar(QScrollBar):
    """Native slider interaction with a thumb-only, transparent overlay."""
    def __init__(self,parent):
        super().__init__(Qt.Orientation.Vertical,parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("QScrollBar {background:transparent;width:6px;margin:0;border:0;}"
            "QScrollBar::handle {min-height:24px;}"
            "QScrollBar::add-line,QScrollBar::sub-line {height:0;border:0;}"
            "QScrollBar::add-page,QScrollBar::sub-page {background:transparent;}")

    def paintEvent(self,event):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        rect = self.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar,option,
            QStyle.SubControl.SC_ScrollBarSlider,self)
        dark = self.palette().color(self.palette().ColorRole.Base).lightness()<128
        color = QColor('#7190af' if dark else '#8da6c2')
        color.setAlpha(220 if self.underMouse() or self.isSliderDown() else 160)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(rect,3,3)


class ScrollBarReveal(QObject):
    IDLE_MS = 900
    FADE_MS = 160

    def __init__(self,area):
        super().__init__(area)
        self.area,self.bar = area,area.verticalScrollBar()
        self.bar.transient_reveal = self
        self.overlay = OverlayScrollBar(area.viewport())
        self.effect = QGraphicsOpacityEffect(self.overlay)
        self.effect.setOpacity(0)
        self.overlay.setGraphicsEffect(self.effect)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(self.IDLE_MS)
        self.timer.timeout.connect(self.fade)
        self.animation = QPropertyAnimation(self.effect,b'opacity',self)
        self.animation.setDuration(self.FADE_MS)
        self.animation.setEasingCurve(QEasingCurve.Type.OutQuad)
        self.animation.finished.connect(self._hidden)
        self.bar.valueChanged.connect(self._value_changed)
        self.bar.rangeChanged.connect(self._range_changed)
        self.bar.sliderPressed.connect(self.reveal)
        self.bar.sliderReleased.connect(self.reveal)
        self.overlay.valueChanged.connect(self.bar.setValue)
        self.overlay.sliderPressed.connect(self.reveal)
        self.overlay.sliderReleased.connect(self.reveal)
        # Keep Qt's hidden scrollbar as the scroll model, reclaim all gutter space.
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sync()

    def sync(self):
        self.overlay.blockSignals(True)
        self.overlay.setRange(self.bar.minimum(),self.bar.maximum())
        self.overlay.setPageStep(self.bar.pageStep())
        self.overlay.setSingleStep(self.bar.singleStep())
        self.overlay.setValue(self.bar.value())
        self.overlay.blockSignals(False)
        viewport = self.area.viewport()
        self.overlay.setGeometry(max(0,viewport.width()-8),2,6,max(0,viewport.height()-4))
        needed = self.bar.maximum()>self.bar.minimum() and not self.area.property('hideScrollIndicator')
        self.overlay.setVisible(needed)
        if needed:
            self.overlay.raise_()
        else:
            self.timer.stop()
            self.animation.stop()
            self.effect.setOpacity(0)
            self._hidden()

    def _value_changed(self,*args):
        self.sync()
        self.reveal()

    def reveal(self,*args):
        if self.bar.maximum()<=self.bar.minimum() or self.area.property('hideScrollIndicator'):
            return
        self.animation.stop()
        self.effect.setOpacity(1)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,False)
        self.overlay.raise_()
        self.timer.start()

    def fade(self):
        if self.overlay.isSliderDown() or self.bar.isSliderDown():
            self.timer.start()
            return
        self.animation.setStartValue(self.effect.opacity())
        self.animation.setEndValue(0)
        self.animation.start()

    def _hidden(self):
        if self.effect.opacity()==0:
            self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)

    def _range_changed(self,*args):
        self.sync()


class TransientScrollBars(QObject):
    """Cover the workbench and its lazily created dialogs and combo popups."""
    def __init__(self,application):
        super().__init__(application)
        application.installEventFilter(self)

    @classmethod
    def attach(cls,root):
        application = QApplication.instance()
        manager = getattr(application,'_sortzip_scrollbars',None)
        if manager is None:
            manager = application._sortzip_scrollbars = cls(application)
        root.setProperty('transientScrollBars',True)
        for area in root.findChildren(QAbstractScrollArea):
            manager.install(area)
        return manager

    @staticmethod
    def install(area):
        bar = area.verticalScrollBar()
        if not hasattr(bar,'transient_reveal'):
            bar.transient_reveal = ScrollBarReveal(area)
        return bar.transient_reveal

    def owns(self,widget):
        # isAncestorOf stops at a window boundary; owned dialogs still belong here.
        while widget is not None:
            if widget.property('transientScrollBars'):
                return True
            widget = widget.parentWidget()
        return False

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Type.Show,QEvent.Type.Polish) and isinstance(watched,QAbstractScrollArea):
            if self.owns(watched):
                self.install(watched)
        elif event.type() in (QEvent.Type.Resize,QEvent.Type.Show):
            parent = watched.parent()
            if isinstance(parent,QAbstractScrollArea) and watched is parent.viewport() and self.owns(parent):
                self.install(parent).sync()
        elif event.type()==QEvent.Type.Wheel:
            parent = watched.parent() if isinstance(watched,QObject) else None
            if isinstance(parent,QAbstractScrollArea) and watched is parent.viewport() and self.owns(parent):
                self.install(parent).reveal()
        return False
