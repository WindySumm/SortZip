"""Workspace drafts, configuration export and preference migration (no Qt UI)."""
from copy import deepcopy
from dataclasses import dataclass, field, asdict
import json
from uuid import uuid4

from sortzip_core.nesting import Layer, Plan, plan_layers
from sortzip_core.formats import LAYER_FORMATS as FORMATS
from sortzip_core.constants import validate_win_folder_name
from sortzip_core.layer_migration import migrate_legacy
from sortzip_core.planning import StepError
from sortzip_core.i18n import tr

SORT_LABELS = {'文件名(升序)':'name','文件名(降序)':'name_desc','修改时间(旧→新)':'mtime',
    '修改时间(新→旧)':'mtime_desc','文件大小(小→大)':'size_asc','文件大小(大→小)':'size_desc','扩展名':'ext'}


@dataclass
class LayerDraft:
    key: str = field(default_factory=lambda:uuid4().hex)
    data: dict = field(default_factory=lambda:dict(group=2,format='zip',volume=False,size='100MB',
        template='{folder}_{n}',encrypt=False,password='',confirm='',verify=False,rename=False,
        suffix='.png',expanded=False,show_password=False,retain=False),repr=False)


def draft_from_layer(layer,required=False):
    return LayerDraft(data=dict(group=layer.group_size,format=layer.archive_format,volume=layer.enable_volume,
        size=layer.volume or '',template=layer.name_template,encrypt=bool(layer.password),
        password=layer.password,confirm=layer.password,verify=layer.verify,rename=layer.suffix is not None,
        suffix=layer.suffix or '.'+layer.archive_format,expanded=False,show_password=False,retain=False))


@dataclass
class PreviewResult:
    revision: int
    entries: list = field(default_factory=list,repr=False)
    file_plan: list = field(default_factory=list,repr=False)
    plan: Plan = field(default_factory=lambda:Plan((),()))
    extensions: tuple = ()
    total_files: int = 0
    file_error: str = ''
    archive_error: str = ''
    prepared: dict | None = field(default=None,repr=False)
    node_errors: dict = field(default_factory=dict)
    inventory: tuple | None = field(default=None,repr=False)
    inventory_key: tuple | None = None
    file_key: str = ''
    file_base: list = field(default_factory=list,repr=False)
    timings: dict = field(default_factory=dict,repr=False)


@dataclass
class WorkspaceState:
    source: str = ''
    destination: str = ''
    mappings: list = field(default_factory=list)
    mapping_enabled: list = field(default_factory=list)
    disabled_extensions: set = field(default_factory=set)
    rules: list = field(default_factory=list)
    rule_enabled: list = field(default_factory=list)
    mapping_ids: list = field(default_factory=list,repr=False)
    rule_ids: list = field(default_factory=list,repr=False)
    layers: list = field(default_factory=lambda:[draft_from_layer(Layer())])
    selected: str = 'mapping'
    revision: int = 0
    recursive: bool = False
    keep_hierarchy: bool = False
    keep_files: bool = False
    output_list: bool = False
    restore_original_names: bool = False
    delivery_manifest: bool = False
    delivery_original_names: bool = False
    compression_enabled: bool = True
    sort_by: str = 'name'
    preview_order: dict = field(default_factory=dict)
    confirm_config: bool = True
    dark_mode: bool = False
    archive_tool: str = 'bandizip'
    archive_tool_paths: dict = field(default_factory=dict)
    invalid_migration: bool = False
    notices: list = field(default_factory=list)
    preview: PreviewResult | None = field(default=None,repr=False)

    def layer_values(self,strict=True):
        values = []
        for index,draft in enumerate(self.layers):
            data = draft.data
            fmt = data['format']
            if fmt not in FORMATS:
                raise StepError(f'layer:{index}',tr('第 {0} 层压缩格式无效', index+1))
            cap = FORMATS[fmt]
            password = data['password'] if cap['password'] else ''
            if strict and password:
                if len(password)<8:
                    raise ValueError(tr('第 {0} 层密码至少需要 8 位；清空密码表示本层不加密', index+1))
                if password!=data['confirm']:
                    raise ValueError(tr('第 {0} 层两次密码不一致', index+1))
            values.append(Layer(data['group'] if cap['multiple'] else 1,fmt,
                data['volume'] and cap['volume'],data['size'].strip() or None,data['template'],
                data['suffix'].strip() if index==len(self.layers)-1 and data['rename'] else None,
                password if strict else '',data['verify']))
        if strict:
            from sortzip_core.archive_tools import validate_tool_layers
            validate_tool_layers(self.archive_tool,values)
            plan_layers((),values,allow_rar=self.archive_tool=='winrar')
        return tuple(values)

    def to_config(self,strict=True):
        mappings = {}
        for index,(ext,folder) in enumerate(self.mappings):
            ext,folder = ext.strip().lower(),folder.strip()
            if not (self.mapping_enabled[index] if index<len(self.mapping_enabled) else ext not in self.disabled_extensions):
                continue
            if ext in ('[无后缀]', '[no extension]'):
                ext = ''
            elif not ext.startswith('.') or len(ext)<2:
                raise StepError('mapping',tr('已启用的映射需要有效扩展名'))
            if validate_win_folder_name(folder):
                raise StepError('mapping',tr('分类目录无效：{0}', folder))
            if ext in mappings:
                raise StepError('mapping',tr('扩展名重复：{0}', ext))
            mappings[ext] = folder
        rules = [dict(enable=self.rule_enabled[index] if index<len(self.rule_enabled) else True,
                      match_folder=folder.strip(),template=template.strip())
                 for index,(folder,template) in enumerate(self.rules) if folder.strip()]
        config = dict(src=self.source.strip(),dest=self.destination.strip(),custom_names=mappings,
            naming_rules=rules,sort_by=self.sort_by,preview_order=deepcopy(self.preview_order),
            recursive=self.recursive,keep_hierarchy=self.keep_hierarchy,keep_files=self.keep_files,
            output_list=self.output_list,first_compress=self.compression_enabled,double_compress=False,
            group_size=1,archive_format='zip',enable_volume=False,password='',
            archive_tool=self.archive_tool,archive_tool_paths=dict(self.archive_tool_paths))
        if self.compression_enabled:
            if strict and self.invalid_migration:
                raise ValueError(tr('旧层配置迁移失败，请先调整压缩层后再执行'))
            values = self.layer_values(strict)
            retained = [index for index,draft in enumerate(self.layers[:-1]) if draft.data.get('retain')]
            config.update(compression_layers=values,group_size=values[0].group_size,archive_format=values[0].archive_format,
                          retain_layers=retained,restore_original_names=self.restore_original_names,
                          delivery_manifest=self.delivery_manifest,
                          delivery_original_names=self.delivery_manifest and self.delivery_original_names)
        return config

    def file_entries(self):
        if self.preview is None or self.preview.revision!=self.revision:
            raise ValueError(tr('预览正在更新，请稍候'))
        if self.preview.file_error:
            raise ValueError(self.preview.file_error)
        return self.preview.entries

    def structure(self):
        entries = self.file_entries()
        if self.preview.archive_error:
            raise ValueError(self.preview.archive_error)
        warnings = []
        if self.compression_enabled:
            try:
                self.layer_values(True)
            except ValueError as error:
                warnings.append(str(error))
        return entries,self.preview.plan,warnings

    def save(self,settings):
        settings.setValue('archive_tool',self.archive_tool)
        settings.setValue('archive_tool_paths',json.dumps(self.archive_tool_paths,ensure_ascii=False))
        for key,value in dict(src=self.source,dest=self.destination,recursive=self.recursive,
                keep_hierarchy=self.keep_hierarchy,keep_files=self.keep_files,output_list=self.output_list,
                restore_original_names=self.restore_original_names,
                delivery_manifest=self.delivery_manifest,delivery_original_names=self.delivery_original_names,
                first_compress=self.compression_enabled,confirm_config=self.confirm_config,
                dark_mode=self.dark_mode,sort_by=next((label for label,code in SORT_LABELS.items() if code==self.sort_by),'文件名(升序)'),
                unified_layers=True,nested_enabled=True,workbench_profile=True,workspace_migration_invalid=self.invalid_migration).items():
            settings.setValue(key,value)
        raw = [dict(key=d.key,data={k:v for k,v in d.data.items() if k not in ('password','confirm','show_password')}) for d in self.layers]
        settings.setValue('workspace_layers',json.dumps(raw,ensure_ascii=False))
        # Retain old preference readers; no password-bearing dataclass is serialized.
        public = []
        for index,draft in enumerate(self.layers):
            d = draft.data
            public.append(dict(group_size=d['group'] if FORMATS[d['format']]['multiple'] else 1,
                archive_format=d['format'],enable_volume=d['volume'] and FORMATS[d['format']]['volume'],
                volume=d['size'] or None,name_template=d['template'],suffix=d['suffix'] if index==len(self.layers)-1 and d['rename'] else None,verify=d['verify']))
        settings.setValue('compression_layers',json.dumps(public,ensure_ascii=False))
        settings.setValue('layer_password_required',json.dumps([bool(d.data['password']) and FORMATS[d.data['format']]['password'] for d in self.layers]))
        settings.setValue('workspace_order',json.dumps(self.preview_order,ensure_ascii=False))
        for name,rows in (('ext_mappings',[dict(ext=e,name=f,checked=self.mapping_enabled[i] if i<len(self.mapping_enabled) else e.strip().lower() not in self.disabled_extensions) for i,(e,f) in enumerate(self.mappings)]),
                          ('naming_rules',[dict(match_folder=f,template=t,enable=self.rule_enabled[i] if i<len(self.rule_enabled) else True) for i,(f,t) in enumerate(self.rules)])):
            settings.beginWriteArray(name,len(rows))
            for index,row in enumerate(rows):
                settings.setArrayIndex(index)
                for key,value in row.items():
                    settings.setValue(key,value)
            settings.endArray()
        settings.sync()


def load_workspace(settings,saved_password=''):
    state = WorkspaceState()
    from sortzip_core.archive_tools import TOOL_NAMES
    tool = settings.value('archive_tool','bandizip',type=str)
    if tool in TOOL_NAMES:
        state.archive_tool = tool
    try:
        paths = json.loads(settings.value('archive_tool_paths','{}',type=str))
        if isinstance(paths,dict):
            state.archive_tool_paths = {key:value for key,value in paths.items() if key in TOOL_NAMES and isinstance(value,str)}
    except (ValueError,TypeError):
        pass
    for field_name,key,default,kind in (
        ('source','src','',str),('destination','dest','',str),('recursive','recursive',False,bool),
        ('keep_hierarchy','keep_hierarchy',False,bool),('keep_files','keep_files',False,bool),
        ('output_list','output_list',False,bool),('compression_enabled','first_compress',True,bool),
        ('restore_original_names','restore_original_names',False,bool),
        ('delivery_manifest','delivery_manifest',False,bool),('delivery_original_names','delivery_original_names',False,bool),
        ('confirm_config','confirm_config',True,bool),('dark_mode','dark_mode',False,bool)):
        setattr(state,field_name,settings.value(key,default,type=kind))
    sort = settings.value('sort_by','文件名(升序)')
    state.sort_by = SORT_LABELS.get(sort,sort if sort in SORT_LABELS.values() else 'name')
    state.invalid_migration = settings.value('workspace_migration_invalid',False,type=bool)
    for name in ('ext_mappings','naming_rules'):
        count = settings.beginReadArray(name)
        try:
            for index in range(count):
                settings.setArrayIndex(index)
                if name=='ext_mappings':
                    ext,folder = str(settings.value('ext','')),str(settings.value('name',''))
                    state.mappings.append((ext,folder))
                    state.mapping_enabled.append(settings.value('checked',False,type=bool))
                    if not settings.value('checked',False,type=bool):
                        state.disabled_extensions.add(ext.strip().lower())
                else:
                    state.rules.append((str(settings.value('match_folder','')),str(settings.value('template',''))))
                    state.rule_enabled.append(settings.value('enable',True,type=bool))
        finally:
            settings.endArray()
    try:
        if settings.value('workbench_profile',False,type=bool):
            rows = json.loads(settings.value('workspace_layers','[]'))
            if not isinstance(rows,list) or not rows:
                raise ValueError(tr('层草稿无效'))
            state.layers = []
            for row in rows:
                data = draft_from_layer(Layer()).data
                data.update({k:v for k,v in row['data'].items() if k in data and k not in ('password','confirm','show_password')})
                for key,default in draft_from_layer(Layer()).data.items():
                    if type(data[key]) is not type(default):
                        raise ValueError(tr('层草稿字段类型无效'))
                if data['format'] not in FORMATS or type(data['group']) is not int or not 1<=data['group']<=9999:
                    raise ValueError(tr('层格式或数量无效'))
                state.layers.append(LayerDraft(data=data))
            state.preview_order = json.loads(settings.value('workspace_order','{}'))
            if not isinstance(state.preview_order,dict):
                state.preview_order = {}
        else:
            saved = json.loads(settings.value('compression_layers','[]'))
            if saved and (settings.value('unified_layers',False,type=bool) or settings.value('nested_enabled',False,type=bool)):
                values = tuple(Layer(**{k:v for k,v in row.items() if k!='password'}) for row in saved)
                plan_layers((),values)
                required = json.loads(settings.value('layer_password_required','[]'))
                prefs = json.loads(settings.value('layer_format_preferences','[]'))
                state.layers = [draft_from_layer(value,index<len(required) and bool(required[index])) for index,value in enumerate(values)]
                for draft,pref in zip(state.layers,prefs):
                    draft.data.update(group=pref.get('group',draft.data['group']),volume=pref.get('volume',draft.data['volume']),encrypt=pref.get('encrypted',draft.data['encrypt']))
            else:
                old = {key:settings.value(key,default,type=kind) for key,default,kind in (
                    ('first_compress',True,bool),('double_compress',False,bool),('group_size',1,int),('archive_format','zip',str),
                    ('enable_volume',False,bool),('volume','',str),('verify_archive',False,bool),('remember_pwd',False,bool),
                    ('first_rename_enable',False,bool),('first_suffix_replace','.zip',str),('archive_rename_enable',False,bool),('archive_suffix','.zip',str))}
                old['password'] = saved_password
                migrated = migrate_legacy(old)
                state.layers = [draft_from_layer(value,needed) for value,needed in zip(migrated.layers,migrated.password_required)]
                state.notices.extend(migrated.notices)
    except (ValueError,TypeError,KeyError,AttributeError):
        state.layers = [draft_from_layer(Layer())]
        state.invalid_migration = True
        state.notices.append(tr('旧层配置无效，请调整压缩层并确认后执行。'))
    # Old enable flags remain readable for profile compatibility; only current
    # session password text controls encryption under the blank-means-off UI.
    for draft in state.layers:
        draft.data['encrypt'] = bool(draft.data['password'])
    return state
