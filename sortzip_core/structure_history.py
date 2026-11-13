"""Session-only structural deltas; no file operations or whole-workspace restore."""
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
from uuid import uuid4
from PySide6.QtCore import QObject,QEvent,Qt
from PySide6.QtGui import QUndoStack,QUndoCommand
from PySide6.QtWidgets import QApplication,QLineEdit,QTextEdit,QPlainTextEdit,QTableWidgetItem
from sortzip_core.workspace_state import LayerDraft

ROW_ID = Qt.ItemDataRole.UserRole+40


def structural(label):
    def decorate(method):
        @wraps(method)
        def wrapped(self,*args,**kwargs):
            history = getattr(self,'structure_history',None)
            if history is None or history.applying or self._loading:
                return method(self,*args,**kwargs)
            with history.transaction(label):
                return method(self,*args,**kwargs)
        return wrapped
    return decorate


class UndoKeys(QObject):
    def eventFilter(self,watched,event):
        if event.type()!=QEvent.Type.KeyPress or not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False
        if event.key() not in (Qt.Key.Key_Z,Qt.Key.Key_Y): return False
        focus = QApplication.focusWidget()
        if isinstance(focus,(QLineEdit,QTextEdit,QPlainTextEdit)) and not focus.isReadOnly():
            return False
        widget = focus
        while widget is not None:
            history = getattr(widget,'structure_history',None)
            if history is not None:
                if not history.window._task_busy():
                    history.stack.undo() if event.key()==Qt.Key.Key_Z else history.stack.redo()
                return True
            widget = widget.parentWidget()
        return False


def patch_collection(before,after):
    old,new = {r['id']:r for r in before},{r['id']:r for r in after}
    removed = [(i,r) for i,r in enumerate(before) if r['id'] not in new]
    added = [(i,r) for i,r in enumerate(after) if r['id'] not in old]
    changed = {}
    for identity in old.keys() & new.keys():
        delta = {key:(old[identity].get(key),value) for key,value in new[identity].items()
                 if key!='id' and old[identity].get(key)!=value}
        if delta: changed[identity] = delta
    order = ([r['id'] for r in before],[r['id'] for r in after]) if not removed and not added and before!=after else None
    return dict(removed=deepcopy(removed),added=deepcopy(added),changed=deepcopy(changed),order=order)


def apply_collection(current,patch,forward,protect_added=False):
    rows = deepcopy(current)
    erase,insert = (patch['removed'],patch['added']) if forward else (patch['added'],patch['removed'])
    for _,row in erase:
        rows = [r for r in rows if r['id']!=row['id'] or
                (protect_added and not forward and r!=row)]
    for index,row in insert:
        if not any(r['id']==row['id'] for r in rows): rows.insert(min(index,len(rows)),deepcopy(row))
    for row in rows:
        for key,values in patch['changed'].get(row['id'],{}).items():
            row[key] = deepcopy(values[1 if forward else 0])
    if patch['order']:
        order = patch['order'][1 if forward else 0]
        by_id = {r['id']:r for r in rows}
        ordered = iter(by_id[i] for i in order if i in by_id)
        rows = [next(ordered) if r['id'] in order else r for r in rows]
    return rows


class StructureCommand(QUndoCommand):
    def __init__(self,history,label,before,after):
        super().__init__(label)
        self.history = history
        self.patches = {key:patch_collection(before[key],after[key]) for key in ('mappings','rules','layers')}
        self.orders = {key:(before['order'].get(key),after['order'].get(key))
                       for key in before['order'].keys() | after['order'].keys()
                       if before['order'].get(key)!=after['order'].get(key)}
        self.selected = before['selected'],after['selected']
        self.protect_rules = before['mappings']!=after['mappings']
        self.first = True

    def redo(self):
        if self.first: self.first = False
        else: self.history.apply(self,True)

    def undo(self): self.history.apply(self,False)


class StructureHistory(QObject):
    def __init__(self,window):
        super().__init__(window)
        self.window = window
        self.stack = QUndoStack(self)
        self.stack.setUndoLimit(100)
        self.applying,self.depth = False,0
        self.before = None
        application = QApplication.instance()
        if not hasattr(application,'_sortzip_undo_keys'):
            application._sortzip_undo_keys = UndoKeys(application)
            application.installEventFilter(application._sortzip_undo_keys)
        for table,label in ((window.mapping_table,'调整映射顺序'),(window.naming.table,'调整规则顺序')):
            table.rowsAboutToReorder.connect(lambda label=label:self.begin(label))
            table.rowsReordered.connect(self.end)
        window.naming.structureStarting.connect(lambda label:self.begin(label))
        window.naming.structureFinished.connect(self.end)
        self.snapshot()
        self.stack.indexChanged.connect(window._sync_action_states)

    def table_rows(self,table):
        rows = []
        blocked = table.blockSignals(True)
        try:
            for index in range(table.rowCount()):
                left,right = table.item(index,0),table.item(index,1)
                if left is None:
                    left = QTableWidgetItem(''); table.setItem(index,0,left)
                identity = left.data(ROW_ID) or uuid4().hex
                left.setData(ROW_ID,identity)
                rows.append(dict(id=identity,left=left.text(),right=right.text() if right else '',
                    enabled=left.checkState()==Qt.CheckState.Checked))
        finally: table.blockSignals(blocked)
        return rows

    def snapshot(self):
        w = self.window
        w.save_layer()
        mappings,rules = self.table_rows(w.mapping_table),self.table_rows(w.naming.table)
        w.state.mapping_ids,w.state.rule_ids = [r['id'] for r in mappings],[r['id'] for r in rules]
        return dict(mappings=mappings,rules=rules,layers=[dict(id=d.key,**deepcopy(d.data)) for d in w.state.layers],
            order=deepcopy(w.state.preview_order),selected=w.state.selected)

    def begin(self,label):
        if self.applying or self.window._loading or self.window._task_busy(): return
        if self.depth==0:
            self.before,self.label = self.snapshot(),label
        self.depth += 1

    def end(self,*args):
        if self.depth==0: return
        self.depth -= 1
        if self.depth==0:
            after = self.snapshot()
            if any(self.before[k]!=after[k] for k in ('mappings','rules','layers','order')):
                self.stack.push(StructureCommand(self,self.label,self.before,after))
            self.before = None
            self.window._sync_action_states()

    @contextmanager
    def transaction(self,label):
        active = not self.applying and not self.window._loading and not self.window._task_busy()
        if active: self.begin(label)
        try: yield
        finally:
            if active: self.end()

    def apply(self,command,forward):
        w = self.window
        if w._task_busy(): return
        current = self.snapshot()
        result = {key:apply_collection(current[key],patch,forward,command.protect_rules and key=='rules')
                  for key,patch in command.patches.items()}
        self.applying = True
        w._loading = True
        try:
            for key,table in (('mappings',w.mapping_table),('rules',w.naming.table)):
                table.setRowCount(0)
                for index,row in enumerate(result[key]):
                    table.insertRow(index)
                    item = w._checked_item(row['left'],row['enabled'])
                    item.setData(ROW_ID,row['id'])
                    table.setItem(index,0,item); table.setItem(index,1,QTableWidgetItem(row['right']))
            w.state.layers = [LayerDraft(key=row['id'],data={k:v for k,v in row.items() if k!='id'}) for row in result['layers']]
            for key,values in command.orders.items():
                value = values[1 if forward else 0]
                if value is None: w.state.preview_order.pop(key,None)
                else: w.state.preview_order[key] = deepcopy(value)
            keys = {d.key for d in w.state.layers} | {'mapping','naming','final'}
            if w.state.selected not in keys:
                preferred = command.selected[1 if forward else 0]
                w.state.selected = preferred if preferred in keys else w.state.layers[-1].key
            # The editor still belongs to the old selection; never save it into restored layers.
            w.rebuild_navigation()
        finally:
            w._loading = False
        try:
            w.naming.fit_table()
            w.change()
            self.snapshot()
        finally: self.applying = False
