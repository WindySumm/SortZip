"""Shared configuration validation and preparation, independent of Qt."""
from pathlib import Path

from sortzip_core.formats import validate_compression
from sortzip_core.constants import RESUME_FILE_NAME
from sortzip_core.planning import build_execution_plan
from sortzip_core.i18n import tr


class ConfigurationError(ValueError):
    def __init__(self, title, message):
        super().__init__(message)
        self.title = title


def validate_password(password, confirm):
    """校验压缩密码，返回错误文案或 None。规则与执行前校验保持一致：非空则至少 8 位、两次输入一致。"""
    if password and len(password) < 8:
        return (tr('密码格式错误'),
                tr('压缩密码至少需要 8 位字符\n支持字母、数字和特殊符号\n如不需要密码请留空'))
    if password != confirm:
        return (tr('密码不一致'), tr('两次输入的压缩密码不一致\n请重新输入'))
    return None



def prepare_config(config, confirmation=None, require_files=False, *, context=None):
    """Validate a normal task and attach a plan without changing caller settings.

    require_files is the interactive policy: reject empty tasks, existing mapped
    categories, and recursive flat tasks with colliding original names.
    Engine callers retain the prior empty-task / existing-dir behavior.
    A confirmed execution_plan is reused; engine checks its source metadata later.
    """
    result = dict(config)
    nested = bool(result.get('first_compress', True) and result.get('compression_layers') is not None)
    if nested:
        from sortzip_core.nested_pipeline import prepare_layers
        layers = prepare_layers(result)
        result['group_size'] = layers[0].group_size
    if not result.get('enable_volume', True):
        result['volume'] = None
    else:
        result['first_suffix_replace'] = None
    if not result.get('double_compress', True):
        result.pop('archive_suffix', None)
    if not result.get('src') or not Path(result['src']).is_dir():
        raise ConfigurationError(tr('输入文件夹无效'), tr('请选择有效的输入文件夹'))
    if not result.get('dest'):
        raise ConfigurationError(tr('输出文件夹无效'), tr('请选择输出文件夹'))
    if Path(result['dest']).exists() and not Path(result['dest']).is_dir():
        raise ConfigurationError(tr('输出文件夹无效'), tr('输出位置已存在且不是文件夹'))
    if (result.get('first_compress',True) or 'archive_tool' in result) and (Path(result['dest'])/RESUME_FILE_NAME).exists():
        raise ConfigurationError(tr('输出已有恢复任务'), tr('输出目录中已有断点，请先恢复原任务或选择其他输出目录，避免覆盖恢复记录。'))
    if not nested:
        validate_compression(result)
    if result.get('first_compress', True) and not nested:
        password = result.get('password', '')
        error = validate_password(password, password if confirmation is None else confirmation)
        if error:
            raise ConfigurationError(*error)
    if require_files and Path(result['dest']).is_dir():
        existing = {p.name.casefold() for p in Path(result['dest']).iterdir() if p.is_dir()}
        overlap = [name for name in (result.get('custom_names') or {}).values() if name.casefold() in existing]
        if overlap:
            raise ConfigurationError(tr('输出文件夹冲突'), tr('输出中已存在映射目录，请清理或修改名称：') + '、'.join(sorted(set(overlap))))
    if result.get('execution_plan') is None:
        result['execution_plan'] = build_execution_plan(result,context=context)
    if require_files and result.get('recursive') and not result.get('keep_hierarchy'):
        seen = set()
        for entry in result['execution_plan']:
            if context: context.check()
            key = (entry['folder'].casefold(), entry['source'].name.casefold())
            if key in seen:
                raise ConfigurationError(tr('重名文件检测'),
                    tr('分类「{0}」中存在多个名为「{1}」的文件。\n请启用「保持原文件夹层级」以保留子目录结构，再执行任务。', entry["folder"], entry["source"].name))
            seen.add(key)
    if require_files and not result['execution_plan']:
        raise ConfigurationError(tr('无匹配文件'), tr('输入文件夹中没有符合已勾选扩展名的文件'))
    if nested:
        from sortzip_core.nested_pipeline import attach_plan
        attach_plan(result,context=context)
    return result
