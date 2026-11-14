"""Translate legacy preferences into one layer configuration, without file I/O.

The unified UI uses the result. Legacy checkpoint readers remain independent.
"""
from dataclasses import dataclass

from sortzip_core.formats import FORMATS
from sortzip_core.nesting import Layer, plan_layers
from sortzip_core.i18n import tr


@dataclass(frozen=True)
class MigratedLayers:
    layers: tuple[Layer, ...]
    enabled: bool
    password_required: tuple[bool, ...]
    notices: tuple[str, ...] = ()


def migrate_legacy(config):
    """A fresh profile gets one visible layer; legacy double mode maps to two.

    password is supplied from an already loaded credential store, never read or
    written as a plaintext setting by this helper.
    """
    fmt = config.get('archive_format','zip')
    if not isinstance(fmt,str) or fmt not in FORMATS:
        raise ValueError(tr('旧设置中的压缩格式不受支持，请重新确认'))
    cap = FORMATS[fmt]
    enabled = config.get('first_compress',True)
    double = config.get('double_compress',False) and enabled
    group = config.get('group_size',1) if cap['multiple'] else 1
    volume = config.get('enable_volume',False) and cap['volume']
    password = config.get('password','') if cap['password'] else ''
    required = cap['password'] and (bool(password) or bool(config.get('remember_pwd',False)))
    verify = config.get('verify_archive',False)
    first_replace = (config.get('first_suffix_replace') if
        config.get('first_rename_enable',False) and not volume else None)
    suffix = first_replace if not double else None
    first = Layer(group,fmt,volume,config.get('volume') or None,
                  '{source_range}-First' if double else '{source_range}',suffix,password,verify)
    layers = [first]
    notices = []
    if double:
        if first_replace:
            notices.append(tr('中间层改后缀设置已取消；新流程仅最外层允许修改后缀。'))
        layers.append(Layer(1,fmt,False,None,'{source_range}',
            config.get('archive_suffix') or '.'+fmt if config.get('archive_rename_enable',False) else None,
            password,verify))
    plan_layers((),layers)
    return MigratedLayers(tuple(layers), bool(enabled), tuple(bool(required) for _ in layers),tuple(notices))
