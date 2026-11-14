"""Small UI primitives with native Qt state and predictable painting/layout."""
from contextlib import contextmanager
from PySide6.QtCore import Qt, QRectF, QSize, QEvent, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QAction, QIcon, QPixmap, QPalette
from PySide6.QtWidgets import (QCheckBox, QStyle, QStyleOptionButton, QStyleOptionFocusRect,
    QWidget, QHBoxLayout, QGridLayout, QSizePolicy, QSpinBox, QToolButton, QLineEdit)
from sortzip_core.i18n import tr


class PasswordVisibility(QAction):
    """One session visibility state, shown at the trailing edge of both fields."""
    def __init__(self,edits,parent):
        super().__init__(parent)
        self.edits = edits
        self.buttons = []
        self.setCheckable(True)
        for edit in edits:
            edit.addAction(self,QLineEdit.ActionPosition.TrailingPosition)
            edit.installEventFilter(self)
            for button in edit.findChildren(QToolButton):
                button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                self.buttons.append(button)
        self.toggled.connect(self.refresh)
        self.refresh()

    def refresh(self,*args):
        visible = self.isChecked()
        label = tr('隐藏密码') if visible else tr('显示密码')
        self.setText(label)
        self.setToolTip(label)
        for edit in self.edits:
            edit.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        for button in self.buttons:
            button.setAccessibleName(label)
        scale = self.edits[0].devicePixelRatioF()
        pixmap = QPixmap(round(20*scale),round(20*scale))
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.edits[0].palette().color(QPalette.ColorRole.Text),1.5,
                            Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        eye = QPainterPath()
        eye.moveTo(2,10)
        eye.cubicTo(6,4,14,4,18,10)
        eye.cubicTo(14,16,6,16,2,10)
        painter.drawPath(eye)
        painter.drawEllipse(QRectF(7.5,7.5,5,5))
        if not visible:
            painter.drawLine(3,3,17,17)
        painter.end()
        self.setIcon(QIcon(pixmap))

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Type.PaletteChange,QEvent.Type.StyleChange,QEvent.Type.DevicePixelRatioChange):
            self.refresh()
        return False


@contextmanager
def loading_controls(owner,*widgets):
    """Nestable loading barrier; restore both Python flags and Qt signals on failure."""
    loading = owner._loading
    capturing = getattr(owner,'_capturing',None)
    owner._loading = True
    if capturing is not None: owner._capturing = True
    blockers = []
    try:
        blockers.extend(QSignalBlocker(widget) for widget in widgets)
        yield
    finally:
        blockers.clear()
        owner._loading = loading
        if capturing is not None: owner._capturing = capturing


class StepSpinBox(QSpinBox):
    """Native number editing with compact, themeable minus/plus buttons."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.lineEdit().setTextMargins(0,0,44,0)
        self.minus = QToolButton(self)
        self.plus = QToolButton(self)
        for button,text,name,step in ((self.minus,'−',tr('减少数量'),-1),(self.plus,'+',tr('增加数量'),1)):
            button.setText(text)
            button.setAccessibleName(name)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setAutoRepeat(True)
            button.clicked.connect(lambda checked=False,delta=step:self.stepBy(delta))
        self.valueChanged.connect(self._sync_steps)
        self._sync_steps()

    def _sync_steps(self):
        self.minus.setEnabled(self.value()>self.minimum())
        self.plus.setEnabled(self.value()<self.maximum())

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.minus.setGeometry(self.width()-46,2,22,self.height()-4)
        self.plus.setGeometry(self.width()-24,2,22,self.height()-4)

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.Type.EnabledChange and hasattr(self,'minus'):
            self._sync_steps()


class StateCheckBox(QCheckBox):
    """Keep QCheckBox interaction intact; paint the state indicator explicitly.

    The Windows style plus scoped QSS can omit its native checked indicator.
    Labels, keyboard handling, accessibility and toggled signals remain Qt-owned.
    """
    def indicator_rect(self):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        return self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator,option,self)

    def paintEvent(self,event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        painter = QPainter(self)
        label_option = QStyleOptionButton(option)
        label_option.state &= ~QStyle.StateFlag.State_HasFocus
        label_option.rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxContents,option,self)
        self.style().drawControl(QStyle.ControlElement.CE_CheckBoxLabel,label_option,painter,self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = option.palette
        roles = palette.ColorRole
        group = palette.ColorGroup.Active if self.isEnabled() else palette.ColorGroup.Disabled
        checked = self.isChecked()
        fill = palette.color(group,roles.Highlight if checked else roles.Base)
        border = palette.color(group,roles.Highlight if checked or self.underMouse() else roles.Dark)
        rectangle = QRectF(self.indicator_rect()).adjusted(.5,.5,-.5,-.5)
        painter.setPen(QPen(border,1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rectangle,3,3)
        if checked:
            # Ensure a visible mark even with an incomplete platform/QSS palette.
            mark = QColor('black') if fill.lightnessF()>.6 else QColor('white')
            pen = QPen(mark,2,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            x,y,w,h = rectangle.x(),rectangle.y(),rectangle.width(),rectangle.height()
            path = QPainterPath()
            if self.checkState()==Qt.CheckState.PartiallyChecked:
                path.moveTo(x+w*.25,y+h*.5)
                path.lineTo(x+w*.75,y+h*.5)
            else:
                path.moveTo(x+w*.22,y+h*.51)
                path.lineTo(x+w*.43,y+h*.73)
                path.lineTo(x+w*.80,y+h*.29)
            painter.drawPath(path)
        if self.hasFocus():
            # Native Windows focus painting can fill the whole label under QSS.
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(palette.color(group,roles.Highlight),1,Qt.PenStyle.DotLine))
            focus = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxFocusRect,option,self)
            painter.drawRoundedRect(QRectF(focus).adjusted(.5,.5,-.5,-.5),2,2)


class CompactFieldRow(QWidget):
    """Two compact label/control groups; surplus space belongs to the row end."""
    def __init__(self,count_label,count_control,format_label,format_control,parent=None):
        super().__init__(parent)
        self.count_label,self.format_label = count_label,format_label
        self.count_control,self.format_control = count_control,format_control
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0,0,0,0)
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(8)
        self.groups = []
        for label,control,width in ((count_label,count_control,100),(format_label,format_control,128)):
            group = QWidget(self)
            layout = QHBoxLayout(group)
            layout.setContentsMargins(0,0,0,0)
            layout.setSpacing(8)
            layout.addWidget(label)
            control.setFixedSize(width,32)
            layout.addWidget(control)
            group.setSizePolicy(QSizePolicy.Policy.Fixed,QSizePolicy.Policy.Preferred)
            self.groups.append(group)
        self._stacked = None
        self.refresh_metrics()

    def refresh_metrics(self):
        metrics = self.count_label.fontMetrics()
        self.count_label.setFixedWidth(max(90,metrics.horizontalAdvance(tr('每包文件数'))+2,
                                           metrics.horizontalAdvance(tr('每包单元数'))+2))
        self.format_label.setFixedWidth(max(64,self.format_label.sizeHint().width()))
        self._reflow()

    def minimumSizeHint(self):
        if not hasattr(self,'groups') or not self.groups:
            return super().minimumSizeHint()
        return QSize(max(group.sizeHint().width() for group in self.groups),72 if self._stacked else 32)

    def _reflow(self):
        needed = sum(group.sizeHint().width() for group in self.groups)+24
        stacked = self.width()<needed
        if stacked==self._stacked:
            return
        self._stacked = stacked
        for group in self.groups:
            self.grid.removeWidget(group)
        self.grid.setColumnStretch(1,1 if stacked else 0)
        self.grid.setColumnStretch(2,0 if stacked else 1)
        self.grid.addWidget(self.groups[0],0,0,Qt.AlignmentFlag.AlignLeft)
        self.grid.addWidget(self.groups[1],1 if stacked else 0,0 if stacked else 1,Qt.AlignmentFlag.AlignLeft)
        self.updateGeometry()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.Type.FontChange and hasattr(self,'groups') and len(self.groups)==2:
            self.refresh_metrics()
