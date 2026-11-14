"""Batch filename preview rendering, preserving identity-based UI state."""
from PySide6.QtCore import Qt,QSignalBlocker,QItemSelectionModel
from PySide6.QtWidgets import QTableWidgetItem
from sortzip_core.i18n import tr


def render_filename_rows(view,rows):
    rows = tuple(rows)
    if rows==view._filename_render_key:
        return False
    table = view.filename_table
    selected = {table.item(index.row(),0).data(Qt.ItemDataRole.UserRole)
        for index in table.selectionModel().selectedRows()}
    current = table.item(table.currentRow(),0)
    identity = current.data(Qt.ItemDataRole.UserRole) if current else None
    scroll = (table.horizontalScrollBar().value(),table.verticalScrollBar().value())
    previous = view.category_filter.currentData()
    updates = table.updatesEnabled()
    table.setUpdatesEnabled(False)
    try:
        with QSignalBlocker(table),QSignalBlocker(view.category_filter):
            table.clearSelection()
            table.setRowCount(len(rows))
            for row,values in enumerate(rows):
                for column,text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    if column==0:
                        item.setData(Qt.ItemDataRole.UserRole,values[0])
                        item.setData(Qt.ItemDataRole.UserRole+1,values[2])
                    table.setItem(row,column,item)
                if values[0] in selected:
                    table.selectionModel().select(table.model().index(row,0),
                        QItemSelectionModel.SelectionFlag.Select|QItemSelectionModel.SelectionFlag.Rows)
                if values[0]==identity:
                    table.setCurrentItem(table.item(row,0),QItemSelectionModel.SelectionFlag.NoUpdate)
            view.category_filter.clear()
            view.category_filter.addItem(tr('全部分类'),None)
            for folder in sorted({row[2] for row in rows}):
                view.category_filter.addItem(folder,folder)
            view.category_filter.setCurrentIndex(max(0,view.category_filter.findData(previous)))
        view.filter_filenames()
        table.horizontalScrollBar().setValue(scroll[0])
        table.verticalScrollBar().setValue(scroll[1])
        view._filename_render_key = rows
    finally:
        table.setUpdatesEnabled(updates)
    return True
