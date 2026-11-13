"""Isolated in-memory example and demo actions using shared presentation."""
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication,QMessageBox
from sortzip_core import __version__
from sortzip_core.constants import validate_win_folder_name
from sortzip_core.formats import FORMATS
from sortzip_core.nesting import Layer,Unit,plan_layers
from sortzip_core.planning import render_template,_match_rule
from sortzip_core.workspace_state import LayerDraft
from sortzip_core.workbench_view import WorkbenchView,WORKBENCH_WIDE_WIDTH


SAMPLE_FILES = ('封面.png', '插画.png', '海报.png', '标志.png', '说明.txt', '笔记.txt')


@dataclass
class PrototypeState:
    """Drafts retain invalid input. Never serialize password-bearing session state."""
    source: str = 'C:\\示例素材'
    destination: str = 'D:\\示例输出'
    mappings: list = field(default_factory=lambda: [('.png','图片'),('.txt','文档')])
    rules: list = field(default_factory=lambda: [('图片','image_{n}{ext}'),('文档','doc_{n}{ext}')])
    layers: list = field(default_factory=lambda: [LayerDraft(),LayerDraft()])
    selected: str = 'mapping'
    revision: int = 0

    def file_entries(self):
        mapping = {}
        for ext,folder in self.mappings:
            if not ext and not folder:
                continue
            if not ext.startswith('.') or not ext[1:] or validate_win_folder_name(folder):
                raise ValueError('分类映射需要有效的扩展名和分类目录')
            if ext.casefold() in mapping:
                raise ValueError('扩展名映射重复')
            mapping[ext.casefold()] = folder
        rules = [dict(match_folder=folder,template=template) for folder,template in self.rules if folder]
        entries, counts, names = [], {}, set()
        for original in SAMPLE_FILES:
            path = PurePosixPath(original)
            folder = mapping.get(path.suffix)
            if not folder:
                continue
            counts[folder] = counts.get(folder,0)+1
            rule = _match_rule(rules,folder)
            target = render_template(rule['template'] if rule else '',counts[folder],path.suffix,folder,path.stem) or original
            if validate_win_folder_name(target):
                raise ValueError('原文件命名生成了无效名称')
            key = (folder.casefold(),target.casefold())
            if key in names:
                raise ValueError('原文件命名产生同目录重名')
            names.add(key)
            entries.append(dict(source=original,folder=folder,target=target))
        return entries

    def structure(self):
        entries = self.file_entries()
        layers, warnings = [], []
        for index,draft in enumerate(self.layers):
            data = draft.data
            fmt = data['format']
            if data['encrypt'] and FORMATS[fmt]['password']:
                if len(data['password'])<8 or data['password']!=data['confirm']:
                    warnings.append(f'第 {index+1} 层密码未确认，不能执行')
            # Password errors do not hide structural preview. Execution is not exposed.
            layers.append(Layer(group_size=data['group'] if FORMATS[fmt]['multiple'] else 1,
                archive_format=fmt,enable_volume=data['volume'] and FORMATS[fmt]['volume'],
                volume=data['size'] or None,name_template=data['template'],verify=data['verify'],
                suffix=data['suffix'] if index==len(self.layers)-1 and data['rename'] else None))
        units = [Unit('source:'+entry['source'],entry['folder'],(entry['source'],)) for entry in entries]
        return entries,plan_layers(units,layers),warnings



class WorkbenchPrototype(WorkbenchView):
    def __init__(self,state=None):
        super().__init__(state if state is not None else PrototypeState())
        self.setWindowTitle(f'SortZip {__version__} · 工作台交互原型')
        self.source_label.setText('示例输入')
        self.destination_label.setText('示例输出')
        self.notice.setText(f'示例任务 · {len(SAMPLE_FILES)} 个文件。路径仅用于展示，不扫描或修改真实文件。')
        self.guide_btn.clicked.connect(self.show_guide)
        self.review_btn.clicked.connect(self.review_summary)

    def show_guide(self):
        QMessageBox.information(self,'原型说明',
            '本窗口只操作示例文件的内存模型，可切换流程、编辑规则、增删层及查看预览。\n'
            '示例路径不会被扫描或写入，不保存密码或偏好；真实执行请使用默认工作台入口。')

    def review_summary(self):
        entries,plan,warnings = self.state.structure()
        message = f'示例任务：{len(entries)} 个文件，{len(self.state.layers)} 层，{len(plan.final_ids)} 个最外层完整单元。\n'
        message += '保留原始文件：'+('是' if self.keep.isChecked() else '否')+'；输出清单：'+('是' if self.output_list.isChecked() else '否')
        self.log.append('已查看示例执行摘要；未操作文件、未调用压缩工具。')
        QMessageBox.information(self,'执行摘要 · 不实际运行',message+'\n\n'+('；'.join(warnings) or '仅用于确认工作台布局和配置上下文。'))


def main():
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont('Microsoft YaHei UI',9))
    window = WorkbenchPrototype()
    screen = app.primaryScreen()
    if screen:
        available = screen.availableGeometry()
        window.resize(min(WORKBENCH_WIDE_WIDTH,available.width()),min(800,available.height()))
    window.show()
    return app.exec()
