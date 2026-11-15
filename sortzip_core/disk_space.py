"""Read-only volume estimates and checks at the point of writing."""
from collections import defaultdict
from pathlib import Path
from shutil import disk_usage

from sortzip_core.i18n import tr


def _existing(path):
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def volume(path):
    existing = _existing(path)
    device = existing.stat().st_dev
    mount = existing
    while mount != mount.parent and mount.parent.stat().st_dev == device:
        mount = mount.parent
    return device, mount


def _size(value):
    if value<1024:
        return f'{value:,} B'
    for unit,divisor in (('GiB',1024**3),('MiB',1024**2),('KiB',1024)):
        if value>=divisor:
            return f'{value/divisor:,.2f} {unit}'


def _shortfall(path, stage, required, free):
    return tr('磁盘 {0} · {1}：可用 {2}，需要 {3}。', path, stage, _size(free), _size(required))


def check_write(path, size, stage, *, estimate=False):
    free = disk_usage(_existing(path)).free
    if size <= free:
        return ''
    message = _shortfall(volume(path)[1], stage, size, free)
    if not estimate or free==0:
        raise ValueError(message)
    print(tr('磁盘空间估算警告：{0}', message))
    return message


def archive_estimate(sizes):
    # Approximate stored payload plus per-member and container overhead.
    return sum(sizes) + 1024 * len(sizes) + 65536


def resume_report(root,plan,available,state,*,copies=()):
    from sortzip_core.retention import SCHEMA
    report = _Estimate()
    for destination,size in copies:
        report.copy(destination,size,strict=False)
    retained = state['retain_layers'] if state['schema']==SCHEMA else None
    completed = state['completed']
    if plan is not None:
        requirements = {child for task in plan.tasks[len(completed):] for child in task.input_ids}
        if retained is not None:
            requirements.update(task.unit.unit_id for task in plan.tasks[:len(completed)]
                                if task.layer_index in retained)
        for identity in requirements & available.keys():
            paths = [Path(path) for path in available[identity].files]
            device = report.locate(paths[0].parent)
            metadata = completed[identity]['metadata'] if identity in completed else state['sources'][identity]
            report.sizes[identity] = device,sum(info[0] for info in metadata.values()),len(paths)
        report.archives(root,plan,retained,state.get('restore_original_names'),completed,
                        staged='publication' in state)
    return report.render()[0]


class _Estimate:
    def __init__(self):
        self.mounts,self.devices,self.sizes,self.free = {},{},{},{}
        self.written,self.next_write = defaultdict(int),{}
        self.peak,self.retained = {},defaultdict(int)

    def locate(self,folder):
        device,mount = self.devices.get(folder) or volume(folder)
        self.devices[folder] = device,mount
        self.mounts[device] = mount
        return device

    def free_on(self,device):
        if device not in self.free:
            self.free[device] = disk_usage(self.mounts[device]).free
        return self.free[device]

    def write(self,device,size,stage):
        self.written[device] += size
        if self.written[device]>self.peak.get(device,(0,''))[0]:
            self.peak[device] = self.written[device],stage

    def copy(self,folder,size,*,source_device=None,strict=True):
        device = self.locate(folder)
        remaining = max(0,self.free_on(device)-self.written[device])
        stage = tr('分类复制')
        if strict and size>remaining:
            raise ValueError(_shortfall(self.mounts[device],stage,size,remaining))
        self.write(device,size,stage)
        self.next_write[device] = self.next_write.get(device,0)+size
        if source_device is not None:
            self.written[source_device] -= size

    def archives(self,root,plan,retained,restore,completed,context=None,*,staged=False):
        from sortzip_core.retention import eligible
        consumers = eligible(plan,retained) if retained is not None else {}
        for task in plan.tasks:
            if context:
                context.check()
            identity = task.unit.unit_id
            level = task.layer_index+1
            if identity in completed:
                if retained is not None and task.layer_index in retained:
                    device,size,_ = self.sizes[identity]
                    self.retained[device,level] += size
                continue
            folder = root if staged else root / f'layer-{level}' / task.unit.folder
            device = self.locate(folder)
            children = [self.sizes[child] for child in task.input_ids]
            payload,count = sum(item[1] for item in children),sum(item[2] for item in children)
            input_bytes = payload
            if restore and level==1:
                payload += 16384+count*512
                count += 3
            size = payload+1024*count+65536
            volumes = 1
            if task.settings.enable_volume:
                from sortzip_core.engine import get_auto_volume
                limit = task.settings.volume or get_auto_volume(input_bytes)
                if limit:
                    value = limit.lower().removesuffix('b')
                    units = {'k':1024,'m':1024**2,'g':1024**3}
                    scale = units.get(value[-1])
                    capacity = int(value[:-1])*scale if scale else int(value)
                    volumes = max(1,(size+capacity-1)//capacity)
            self.sizes[identity] = device,size,volumes
            self.next_write.setdefault(device,size)
            self.write(device,size,tr('第 {0} 层压缩',level))
            if retained is not None and task.layer_index in retained:
                self.retained[device,level] += size
            for child in task.input_ids:
                if consumers.get(child)==identity:
                    child_device,child_size,_ = self.sizes[child]
                    self.written[child_device] -= child_size

    def render(self):
        rows,warning = [],False
        for device,mount in self.mounts.items():
            free = self.free_on(device)
            required,stage = self.peak.get(device,(0,tr('无额外写入')))
            warning |= required>free
            rows.append(tr('磁盘 {0}：可用 {1}，下一阶段预计写入 {2}，预计峰值额外占用 {3}（{4}）。',
                           mount,_size(free),_size(self.next_write.get(device,0)),_size(required),stage))
            retained = sorted(((size,level) for (disk,level),size in self.retained.items() if disk==device),reverse=True)
            if retained:
                rows.append(tr('预计占用最大的保留内层：{0}。',', '.join(
                    tr('第 {0} 层约 {1}',level,_size(size)) for size,level in retained[:3])))
        if warning:
            rows.append(tr('预计磁盘空间偏紧。压缩大小和临时占用会变化，是否仍要尝试？'))
        return '\n'.join(rows),warning


def inspect(config, *, context=None):
    """Return the initial space report and whether estimated headroom is low."""
    root = Path(config['dest'])
    report = _Estimate()
    plan = config.get('_nested_plan')
    keep = config.get('retain_layers')
    for entry in config['execution_plan']:
        if context:
            context.check()
        folder = root / entry['folder']
        device = report.locate(folder)
        size = entry['size']
        source_device = entry['source'].stat().st_dev
        if config.get('keep_files') or source_device != device:
            report.copy(folder,size,source_device=None if config.get('keep_files') else source_device)
        report.sizes['source:'+entry['source_rel']] = device,size,1
    if plan:
        report.archives(root,plan,keep,config.get('restore_original_names'),{},context,staged=True)
    return report.render()
