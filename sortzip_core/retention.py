"""Replayable deletion of task-owned inputs after their last consumer commits."""
from pathlib import Path

from sortzip_core import file_journal as files
from sortzip_core.i18n import tr


SCHEMA = 5


def unverified_cleanup(plan,state):
    completed = state['completed']
    return {unit for unit,consumer in eligible(plan,state['retain_layers']).items()
            if consumer in completed and completed[consumer].get('members_verified') is not True
            and state['cleanup'].get(unit)!='done'}


def eligible(plan, retained):
    """Map each releasable unit to the last task that uses it."""
    protected = set(plan.final_ids)
    protected.update(task.unit.unit_id for task in plan.tasks if task.layer_index in retained)
    return {child: task.unit.unit_id for task in plan.tasks for child in task.input_ids
            if child not in protected}


def validate(state, plan):
    retained, cleanup = state.get('retain_layers'), state.get('cleanup')
    outermost = plan.tasks[-1].layer_index if plan.tasks else 0
    if (not isinstance(retained,list) or any(type(i) is not int or i<0 or i>=outermost
            for i in retained) or len(set(retained))!=len(retained) or not isinstance(cleanup,dict)):
        raise ValueError(tr('断点产物保留设置无效'))
    consumers = eligible(plan,retained)
    if any(consumers.get(unit) not in state['completed'] or step not in ('pending','done')
           for unit,step in cleanup.items()):
        raise ValueError(tr('断点清理记录无效'))
    return cleanup


def archive_identity(record):
    raw = record.get('identities')
    if (not isinstance(raw,dict) or raw.keys()!=record['metadata'].keys() or any(
            not isinstance(value,list) or len(value)!=4 or
            any(type(number) is not int or number<0 for number in value) or not value[1] or
            value[2:]!=record['metadata'][name] for name,value in raw.items())):
        raise ValueError(tr('断点归档文件身份无效'))
    return raw


class Cleanup:
    def __init__(self,root,plan,state,operations,db,save):
        self.root,self.state,self.save = root,state,save
        self.consumers = eligible(plan,state['retain_layers'])
        self.eligible_by_consumer = {}
        for unit,owner in self.consumers.items():
            self.eligible_by_consumer.setdefault(owner,[]).append(unit)
        self.sources = {'source:'+row['source_rel']:item for row,item in zip(state['file_plan'],operations,strict=True)}
        self.original_ids = files.ready_identities(db,operations)

    def release(self,consumer):
        if self.state['completed'][consumer].get('members_verified') is not True:
            return
        log = self.state['cleanup']
        units = [unit for unit in self.eligible_by_consumer.get(consumer,()) if log.get(unit)!='done']
        if not units:
            return
        targets = [target for unit in units for target in self._checked_targets(unit,log.get(unit)=='pending')]
        if any(log.get(unit)!='pending' for unit in units):
            log.update(dict.fromkeys(units,'pending'))
            self.save(self.root,self.state)
        for path,expected in targets:
            path = files.safe_path(self.root,path.relative_to(self.root).as_posix())
            if files.identity(path)!=expected:
                raise ValueError(tr('清理目标缺失或已替换：{0}', path))
            path.unlink()
            files._sync_dir(path.parent)
        log.update(dict.fromkeys(units,'done'))
        self.save(self.root,self.state)

    def replay(self,cancel_check=None):
        for consumer in self.state['completed']:
            if cancel_check and cancel_check():
                return
            self.release(consumer)

    def _targets(self,unit):
        if unit in self.sources:
            item = self.sources[unit]
            path = files.safe_path(self.root,item.target.relative_to(self.root).as_posix())
            return [(path,(*self.original_ids[item.index],*item.source_id[2:]),item)]
        record = self.state['completed'][unit]
        return [(files.safe_path(self.root,record['folder']+'/'+name),tuple(value),None)
                for name,value in archive_identity(record).items()]

    def _checked_targets(self,unit,pending):
        for path,expected,source in self._targets(unit):
            if not files.exists(path) and pending:
                continue
            if not files.exists(path) or files.identity(path)!=expected:
                raise ValueError(tr('清理目标缺失或已替换：{0}', path))
            if source is not None:
                source.check(path,expected[:2])
            yield path,expected
