"""Declarative multi-layer plans. No file operations or compressor invocation.

Group complete units first; materialize their files only at execution time.
"""
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sortzip_core.constants import validate_win_folder_name
from sortzip_core.formats import LAYER_FORMATS as FORMATS, validate_compression
from sortzip_core.planning import render_template,StepError


@dataclass(frozen=True)
class Unit:
    unit_id: str
    folder: str
    original_ids: tuple[str, ...]


@dataclass(frozen=True)
class Layer:
    group_size: int = 1
    archive_format: str = 'zip'
    enable_volume: bool = False
    volume: str | None = None
    name_template: str = '{original}'
    suffix: str | None = None
    password: str = field(default='', repr=False)
    verify: bool = False


@dataclass(frozen=True)
class Task:
    unit: Unit
    layer_index: int
    input_ids: tuple[str, ...]
    archive_stem: str
    settings: Layer


@dataclass(frozen=True)
class Plan:
    tasks: tuple[Task, ...]
    final_ids: tuple[str, ...]


def plan_layers(inputs, layers, *, context=None, allow_rar=False):
    """Keep input order within each folder; never merge distinct folder scopes.

    Volume counts are deliberately absent: they are known after compression.
    Each group's entire output becomes one input unit for the following layer.
    """
    units, layers = tuple(inputs), tuple(layers)
    if not layers:
        raise ValueError('请至少添加一层压缩')
    known, originals, scope_ranks = set(), set(), {}
    for unit in units:
        if context: context.check()
        if not isinstance(unit.unit_id, str) or not unit.unit_id or unit.unit_id in known:
            raise ValueError('输入单元 ID 无效或重复')
        known.add(unit.unit_id)
        if (not isinstance(unit.folder, str) or not unit.folder or '\\' in unit.folder
                or any(validate_win_folder_name(part) for part in unit.folder.split('/'))):
            raise ValueError('单元目录必须是安全的相对路径')
        if not isinstance(unit.original_ids, tuple) or not unit.original_ids:
            raise ValueError('单元缺少原始文件身份')
        for identity in unit.original_ids:
            if not isinstance(identity, str) or not identity or identity in originals:
                raise ValueError('原始文件身份无效或跨单元重复')
            originals.add(identity)
            ranks = scope_ranks.setdefault(unit.folder.casefold(), {})
            ranks[identity] = len(ranks)+1
    for index, layer in enumerate(layers):
        try:
            if any(type(value) is not bool for value in (layer.enable_volume, layer.verify)):
                raise ValueError('分卷和校验开关必须为布尔值')
            if not isinstance(layer.name_template, str) or not isinstance(layer.password, str):
                raise ValueError('命名模板和密码必须为字符串')
            if layer.suffix is not None and index != len(layers)-1:
                raise ValueError('仅最外层允许修改后缀')
            validate_compression(dict(first_compress=True, archive_format=layer.archive_format,
                group_size=layer.group_size, enable_volume=layer.enable_volume,
                volume=layer.volume, password=layer.password, double_compress=True,
                archive_suffix=layer.suffix),allow_rar=allow_rar)
            if layer.password and len(layer.password) < 8:
                raise ValueError('压缩密码至少需要 8 位字符')
        except ValueError as error:
            raise StepError(f'layer:{index}',str(error)) from error
    tasks = []
    for layer_index, layer in enumerate(layers):
        if context: context.check()
        folders = {}
        for unit in units:
            folders.setdefault(unit.folder.casefold(), []).append(unit)
        outputs = []
        for folder_index, members in enumerate(folders.values()):
            names = set()
            for start in range(0, len(members), layer.group_size):
                if context: context.check()
                group = members[start:start+layer.group_size]
                end = start + len(group)
                base = str(start+1) if end == start+1 else f'{start+1}-{end}'
                ordinal = start//layer.group_size+1
                ranks = scope_ranks[group[0].folder.casefold()]
                positions = [ranks[identity] for child in group for identity in child.original_ids]
                lo, hi = min(positions), max(positions)
                source_range = str(lo) if lo==hi else f'{lo}-{hi}'
                stem = render_template(layer.name_template.replace('{source_range}',source_range), ordinal, '',
                    PurePosixPath(group[0].folder).name, base) or base
                if validate_win_folder_name(stem) or any(c in stem for c in '*?[]'):
                    raise StepError(f'layer:{layer_index}','压缩包命名模板生成了无效名称')
                if stem.casefold() in names:
                    raise StepError(f'layer:{layer_index}','同层同目录的压缩包名称重复')
                names.add(stem.casefold())
                identity = f'layer:{layer_index+1}:folder:{folder_index+1}:group:{ordinal}'
                if identity in known:
                    raise ValueError('生成单元 ID 与输入冲突')
                known.add(identity)
                output = Unit(identity, group[0].folder,
                    tuple(original for child in group for original in child.original_ids))
                tasks.append(Task(output, layer_index, tuple(u.unit_id for u in group), stem, layer))
                outputs.append(output)
        units = tuple(outputs)
    return Plan(tuple(tasks), tuple(u.unit_id for u in units))


@dataclass(frozen=True)
class MaterializedUnit:
    """An executor publishes a unit only after discovering all of its artifacts."""
    files: tuple[str, ...]
    complete: bool = False


def resolve_inputs(task, available):
    """Resolve whole artifact sets, refusing partial units and incompatible streams.

    Paths are opaque here; the executor must independently validate paths, source
    metadata, artifact completeness and checkpoint persistence before publication.
    """
    files, seen = [], set()
    for identity in task.input_ids:
        unit = available.get(identity)
        if (unit is None or unit.complete is not True or not isinstance(unit.files, tuple)
                or not unit.files):
            raise ValueError(f'输入单元尚未完整完成：{identity}')
        for path in unit.files:
            if not isinstance(path, str) or not path or path.casefold() in seen:
                raise ValueError('输入产物路径无效或跨单元重复')
            seen.add(path.casefold())
            files.append(path)
    if not FORMATS[task.settings.archive_format]['multiple'] and len(files) != 1:
        raise ValueError('该格式只能接收一个实体文件，不能打包包含多卷的单元')
    return tuple(files)
