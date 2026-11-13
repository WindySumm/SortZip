"""Workbench presentation controls; all interaction remains Qt-owned."""
from enum import IntEnum
from math import ceil

from PySide6.QtCore import Qt, QEvent, QRectF, QPointF
from PySide6.QtGui import QFont, QFontMetrics, QColor, QPainter, QPainterPath, QPixmap, QTransform, QPen, QPalette, QBrush
from PySide6.QtWidgets import (QComboBox, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QApplication)


class NodeRole(IntEnum):
    Title = Qt.ItemDataRole.UserRole+1
    Summary = Qt.ItemDataRole.UserRole+2
    LayerNumber = Qt.ItemDataRole.UserRole+3
    Outer = Qt.ItemDataRole.UserRole+4
    Error = Qt.ItemDataRole.UserRole+5


class DigitTiles:
    """Ten vector glyphs per font, reusable themed/DPI-aware numeral tiles."""
    def __init__(self):
        self.paths = {}
        self.tiles = {}

    @staticmethod
    def text(number):
        return f'{number:02d}'

    def glyphs(self,font):
        font = QFont(font)
        font.setPixelSize(30)
        font.setWeight(QFont.Weight.Bold)
        key = font.toString()
        if key not in self.paths:
            raw = []
            for digit in '0123456789':
                path = QPainterPath()
                path.addText(0,0,font,digit)
                path = QTransform().shear(-.12,0).map(path)
                raw.append(path)
            maximum = max(path.boundingRect().width() for path in raw)
            paths = []
            for path in raw:
                bounds = path.boundingRect()
                offset = 1+(maximum-bounds.width())/2
                paths.append(QTransform().translate(offset-bounds.left(),1-bounds.top()).map(path))
            width = ceil(maximum)+5
            height = ceil(max(p.boundingRect().bottom() for p in paths))+5
            self.paths[key] = paths,width,height
        return key,self.paths[key]

    def tile(self,font,digit,ratio,color):
        font_key,(paths,width,height) = self.glyphs(font)
        key = (font_key,digit,ratio,color.name())
        if key not in self.tiles:
            pixmap = QPixmap(ceil(width*ratio),ceil(height*ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = paths[int(digit)]
            painter.translate(2,3)
            painter.fillPath(path,QColor(0,0,0,28))
            painter.translate(-1,-2)
            side = color.darker(135)
            side.setAlpha(150)
            painter.fillPath(path,side)
            painter.translate(-1,-1)
            painter.fillPath(path,color)
            painter.end()
            self.tiles[key] = pixmap
        return self.tiles[key],width,height

    def paint(self,painter,rect,number,font,color):
        ratio = painter.device().devicePixelRatioF()
        text = self.text(number)
        tiles = [self.tile(font,digit,ratio,color) for digit in text]
        width,height = tiles[0][1:]
        scale = min(1.,rect.width()/(width*len(text)),rect.height()/height)
        painter.save()
        painter.translate(rect.center().x()-width*len(text)*scale/2,rect.center().y()-height*scale/2)
        painter.scale(scale,scale)
        for index,(tile,_,_) in enumerate(tiles):
            painter.drawPixmap(QPointF(index*width,0),tile)
        painter.restore()


class FlowNavigationDelegate(QStyledItemDelegate):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.digits = DigitTiles()

    @staticmethod
    def regions(rect,number):
        content = QRectF(rect).adjusted(12,0,-12,0)
        width = content.width()-(52 if number else 0)
        return (QRectF(content.left(),content.top()+14,width,23),
                QRectF(content.left(),content.top()+43,width,19),
                QRectF(content.right()-44,content.top()+30,44,34),
                QRectF(content.right()-44,content.top()+8,44,16))

    def paint(self,painter,option,index):
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option,index)
        option.state &= ~QStyle.StateFlag.State_HasFocus
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        option.text = ''
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem,option,painter,option.widget)
        number = index.data(NodeRole.LayerNumber)
        title,summary,digits,badge = self.regions(option.rect,number)
        dark = option.palette.color(option.palette.ColorRole.Base).lightness()<128
        accent = QColor('#8bc5ff' if dark else '#0865be')
        painter.save()
        painter.setClipRect(option.rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(option.font)
        font.setPixelSize(14)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(accent if selected else option.palette.color(option.palette.ColorRole.Text))
        text = QFontMetrics(font).elidedText(index.data(NodeRole.Title) or '',Qt.TextElideMode.ElideRight,int(title.width()))
        painter.drawText(title,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,text)
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor('#a3b1c2' if dark else '#63758b'))
        text = QFontMetrics(font).elidedText(index.data(NodeRole.Summary) or '',Qt.TextElideMode.ElideRight,int(summary.width()))
        painter.drawText(summary,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,text)
        if number:
            color = QColor(('#83a9d0' if selected else '#61768f') if dark else ('#6e9ac8' if selected else '#9caec4'))
            self.digits.paint(painter,digits,number,option.font,color)
        if index.data(NodeRole.Outer):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#243e59' if dark else '#e7f1ff'))
            painter.drawRoundedRect(badge,3,3)
            font.setPixelSize(10)
            painter.setFont(font)
            painter.setPen(accent)
            painter.drawText(badge,Qt.AlignmentFlag.AlignCenter,'最外层')
        if index.data(NodeRole.Error):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#f0a864' if dark else '#bd6020'))
            painter.drawEllipse(QPointF(option.rect.left()+6,option.rect.top()+10),3,3)
        painter.restore()


class PreviewItemDelegate(QStyledItemDelegate):
    """Flat selection for read-only previews, independent of native accent paint."""
    def paint(self,painter,option,index):
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option,index)
        option.state &= ~QStyle.StateFlag.State_HasFocus
        if not option.state & QStyle.StateFlag.State_Selected:
            super().paint(painter,option,index)
            return
        background = option.palette.color(QPalette.ColorRole.Highlight)
        foreground = option.palette.color(QPalette.ColorRole.HighlightedText)
        option.state &= ~QStyle.StateFlag.State_Selected
        option.features &= ~QStyleOptionViewItem.ViewItemFeature.Alternate
        option.backgroundBrush = QBrush(background)
        option.palette.setColor(QPalette.ColorRole.Base,background)
        option.palette.setColor(QPalette.ColorRole.AlternateBase,background)
        option.palette.setColor(QPalette.ColorRole.Text,foreground)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem,option,painter,option.widget)


class QuietFocusComboBox(QComboBox):
    """Neutral mouse focus, a keyboard-only hint, and native popup semantics."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self._keyboard_mode = False
        self.setProperty('keyboardFocus',False)
        self.view().installEventFilter(self)
        self.view().viewport().installEventFilter(self)

    def _hint(self):
        visible = bool(self._keyboard_mode and self.hasFocus() and self.isEnabled())
        if self.property('keyboardFocus')!=visible:
            self.setProperty('keyboardFocus',visible)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def focusInEvent(self,event):
        super().focusInEvent(event)
        if event.reason() in (Qt.FocusReason.TabFocusReason,Qt.FocusReason.BacktabFocusReason,Qt.FocusReason.ShortcutFocusReason):
            self._keyboard_mode = True
        elif event.reason() not in (Qt.FocusReason.PopupFocusReason,Qt.FocusReason.ActiveWindowFocusReason):
            self._keyboard_mode = False
        self._hint()

    def focusOutEvent(self,event):
        super().focusOutEvent(event)
        if event.reason() not in (Qt.FocusReason.PopupFocusReason,Qt.FocusReason.ActiveWindowFocusReason):
            self._keyboard_mode = False
        self._hint()

    def mousePressEvent(self,event):
        self._keyboard_mode = False
        self._hint()
        super().mousePressEvent(event)

    def keyPressEvent(self,event):
        self._keyboard_mode = True
        self._hint()
        super().keyPressEvent(event)

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Type.MouseButtonPress,QEvent.Type.KeyPress):
            self._keyboard_mode = event.type()==QEvent.Type.KeyPress
            self._hint()
        return super().eventFilter(watched,event)

    def hidePopup(self):
        super().hidePopup()
        self._hint()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.Type.EnabledChange and hasattr(self,'_keyboard_mode'):
            self._hint()

    def paintEvent(self,event):
        # Paint one flat frame; the Windows complex control adds beveled edges
        # even with focus flags stripped. Qt still owns popup/input handling.
        palette = self.palette()
        dark = palette.color(QPalette.ColorRole.Base).lightness()<128
        background = palette.color(QPalette.ColorRole.Base)
        foreground = palette.color(QPalette.ColorRole.Text)
        if not self.isEnabled():
            background = palette.color(QPalette.ColorRole.Window)
            foreground = QColor('#a7b6c8' if dark else '#60728a')
        elif self.property('keyboardFocus'):
            background = QColor('#2a3b50' if dark else '#edf4fc')
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#3c4a5d' if dark else '#d9e1eb'),1))
        painter.setBrush(background)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),4,4)
        text_rect = QStyle.visualRect(self.layoutDirection(),self.rect(),self.rect().adjusted(8,0,-28,0))
        if not self.isEditable():
            icon = self.itemIcon(self.currentIndex())
            if not icon.isNull():
                size = self.iconSize()
                icon_rect = QRectF(text_rect.left(),(self.height()-size.height())/2,size.width(),size.height())
                icon.paint(painter,icon_rect.toRect())
                text_rect.adjust(size.width()+4,0,0,0)
            painter.setFont(self.font())
            painter.setPen(foreground)
            text = self.fontMetrics().elidedText(self.currentText() or self.placeholderText(),Qt.TextElideMode.ElideRight,text_rect.width())
            painter.drawText(text_rect,Qt.AlignmentFlag.AlignLeading|Qt.AlignmentFlag.AlignVCenter,text)
        x = 13 if self.layoutDirection()==Qt.LayoutDirection.RightToLeft else self.width()-13
        y = self.height()/2
        arrow = QPainterPath()
        arrow.moveTo(x-4,y-2)
        arrow.lineTo(x,y+2)
        arrow.lineTo(x+4,y-2)
        painter.setPen(QPen(foreground,1.25,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(arrow)
