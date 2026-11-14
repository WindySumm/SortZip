# SortZip 开发与验证

## v0.9.2：第二轮 UI 优化

融合式界面已接入正式工作台，辅助操作收纳为全局、任务和节点菜单，常用输入保留，所有入口共用 QAction。这部分开发成果随 v1.0.0 交付。

布局优先通过结构表达含义，减少不必要的次级标签和说明。滑块使用覆盖层，不通过预留滚动条槽改变布局；左侧选中背景完整铺满。修改必须保留分类、命名、多层参数、预览、执行、取消和恢复的原行为。

每次功能或操作变化同步 README、CHANGELOG 的 Unreleased 和内置手册；无用户可见变化时只更新适用记录。大版本合为一次本地版本提交，需用户明确下令；提交、推送和发布分别授权。验证产物写入 `.build-validation/`，发布附件保存在 `release/`。

沿用的验证基线为 244 项完整回归（含融合式界面测试）；这是上一阶段证据，不自动代表后续新增行为已验收。原生验收范围与缺口单独记录在迁移基线中。

## 融合式界面实现

`workbench_view.WorkbenchView` 提供没有示例数据、扫描或执行副作用的布局、命名和预览组件。正式 `WorkbenchWindow` 直接创建最终控件，`WorkbenchPrototype` 只提供内存示例和示例动作，两者不再存在生产继承示例的关系。LayerRow 的构建参数决定是否创建层操作及内联模板，不通过先创建再删除控件适配正式布局。

`workbench_polish.WorkbenchPolishMixin` 为正式入口创建共享动作、菜单、线性图标和主题。动作集合使用 `commands`，不覆盖 QWidget.actions()；信号只连接一次。状态加载使用加载屏障，首次后台预览在控件、动作和控制器就绪后启动。参数仍由 WorkspaceState 导出，不通过隐藏旧按钮控制配置；Qt 父对象拥有控件，关闭等待后台线程安全退出后才由主线程销毁。

共享动作按选中步骤及忙碌状态同步；任务菜单按钮沿用 DropResumeButton 的拖入信号。运行期间可看步骤、日志和手册，配置与恢复禁用，取消和进度可见。

## 工作台架构（沿用 v0.9.1）

默认 `main.py` 及 `app.MainWindow` 使用 `workbench.WorkbenchWindow`。`--workbench-prototype` 仅保留独立示例。迁移契约见 [工作台迁移基线](docs/WORKBENCH_MIGRATION.md)。

`workspace_state` 保存草稿与旧偏好迁移，`workspace_planning` 负责不依赖 Qt 的只读扫描/断点检查，`workspace_services` 提供可取消的单线程后台队列，`workspace_tasks` 统一预检、确认、执行及线程释放。控件修改模型，执行只读取模型快照；不从隐藏旧界面导出配置。

`window_fit.WindowFitMixin` 按当前屏幕的可用逻辑区域减去实际窗口边框约束几何位置；屏幕及 DPI 变化后排队复核。常规保持 640×600 最小设计尺寸，低高度下释放窗口强制最小高度，中间工作区整体滚动，目录和底部执行区固定。

预览清单为不可变 `SourceRecord` 元数据集合，缓存键是输入根目录及递归选项；输出子目录排除在使用清单时应用，修改输出目录不会沿用曾被过滤的清单。`build_execution_plan(..., inventory=...)` 只在预览显式复用元数据，默认引擎读取及校验保持原样。文件计划与归档计划分开复用；展示和密码确认仅更新执行条件。队列最多一项运行、一项最新待处理，取消和修订编号共同阻止旧结果回写。手动刷新和任务结束使清单失效；严格执行预检忽略全部缓存。无外部目录监听。

`structure_history` 用 QUndoStack 记录结构差异和稳定行／层身份，仅记录受影响条目、字段及顺序，不持有完整工作区快照。关联自动规则纳入同一步，后来编辑过的生成规则在撤销映射添加时保留。删除层的草稿和密码只存于会话命令内，历史不进入设置、日志或断点。最多 100 步；文本编辑器优先原生撤销，其他控件使用结构快捷键。恢复断点的临时视图不改原历史，重置设置清空历史。

生成文件的扫描／计划基准：`.venv/Scripts/python.exe scripts/benchmark_preview.py`，结果写入已忽略的 `.build-validation/preview-benchmark.json`。计时受磁盘及并发负载影响，扫描次数为强制断言，不把单次计时视为性能承诺。

本轮回归增至 266 项，保留原 256 项业务断言；150%／200% 的离屏工作台各 63 项通过。源码与临时冻结 EXE 启动报告均为 status=ok、version=v0.9.2。不要将离屏和引擎集成测试描述为原生验收通过。

解锁后原生复测覆盖安全取消、连续任务、运行中关闭、重启恢复、真实 Bandizip 进程强制取消及无效断点；三份恢复产物均逐层解压核对。200% DPI 深色低高度窗口已实际滚动到文件名预览，目录和执行区固定可见。修复深色弹窗和恢复视图主题问题；全部主题×尺寸×DPI 的交叉矩阵及真实双屏热切换仍未完成，范围记录在迁移基线。`scripts/verify_acceptance.py OUTPUT SOURCE` 对已完成的隔离两层任务逐层解压、核对文件字节及清单中的原名、新名和所属归档，报告写入素材目录；不修改归档或源文件。

`preview_coordinator.PreviewCoordinator` 集中管理预览签名、缓存、提交修订和结果应用；`preview_display` 批量填充文件名表，以源身份保持选择、筛选和滚动位置，相同数据不重建行。切换步骤只更新步骤树，密码确认只更新执行条件。原型继续复用共享展示。

`planning_context.PlanningContext` 通过可选关键字 `context` 在本次规划中传递取消检查及路径解析上下文，不进入配置、偏好或断点。预览缓存共同父目录，现存叶节点通过 lstat 检查符号链接／Windows 重解析属性；严格预检禁用路径缓存，默认引擎调用不传上下文。复用文件名计划时仍重查输出目录拓扑；归档注释写入独立行副本，不污染文件名缓存。

复测基准可用 `scripts/benchmark_preview.py --materials 已有素材目录 --repeats 3`，默认报告为 `.build-validation/preview-optimized.json`，与旧报告分别保存。报告含扫描次数、扫描／文件计划／归档计划阶段、界面填充和同结果展示耗时。完整解析对照使用相同规划器关闭预览路径缓存，明确不等于旧版本代码复刻；三次中位数一万文件缓存重算约 0.98 秒，对照约 4.38 秒，改善约 78%，完整填充约 0.37 秒、同结果展示约 0.0051 秒。首次约 2.05 秒、严格预检约 5.45 秒；固定延迟不是产品承诺。

原 GUI 保存在 `tests/legacy_ui_baseline.py`，只供历史回归断言比较，不被生产入口导入。实际工作台回归位于 `tests/test_workbench.py`，覆盖真实执行、旧/新恢复参数、反复切换、过期结果、1000 文件快速修改以及线程释放。Qt 测试等待用 processEvents 加短暂 Python sleep 释放 GIL，避免 qWait 使 Python 后台扫描饥饿。线程 finished 后异步轮询 wait(0)，确认原生清理完成后再释放 Python 引用；不阻塞 UI。

一万文件每包 1 文件的补测也使用三次中位数：缓存重算约 2.33 秒，完整解析对照约 8.80 秒，改善约 73%。可运行基准并加 `--counts 10000 --group-size 1 --report .build-validation/preview-many-archives.json` 复测，单独保存报告；不会把每包 100 文件的旧基线用于该分组的严格耗时门槛。

## 环境与安装

界面语言在启动时取自 `ui_language` 偏好。跟随系统时按 Qt 提供的首选显示语言顺序匹配简体中文或英语，其他语言回退英语。翻译条目保存在 `sortzip_core/translations/en.json`，英文手册在 `sortzip_core/manual_en.py`。新增显示文案时使用 `tr()`，动态内容以编号占位符传入。保存的分类、命名规则和清单列名有自己的数据格式。

```text
SortZip/
├─ sortzip_core/        应用源码和运行资源
├─ assets/              Windows 图标
├─ docs/                公开文档、README 图片及 Logo 源图
├─ tests/               可重复运行的测试
├─ scripts/             打包、手动验收和性能基准入口
├─ .build-validation/  本地验证报告与按需生成的素材
└─ release/             发布用 EXE 和 SHA-256 校验文件
```

Git 跟踪源码、文档、资源和测试脚本。`.build-validation/`、`release/` 和 `.venv/` 只供本地使用。

当前验证基线为 **Windows 11 x64、CPython 3.14.2、PySide6 6.11.1、Bandizip 7.46.0.1**。打包工具为 PyInstaller 6.22.3。其他组合尚未完成同等验证。

在项目根目录使用 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe main.py
```

只运行源码时安装 `requirements.txt`，其中 `PySide6-Essentials` 提供应用和测试使用的 Qt 模块。打包使用 `requirements-dev.txt`。已有完整 PySide6 环境也可运行。如需精简，先卸载 `PySide6` 和 `PySide6-Addons`，再用 `pip install --force-reinstall -r requirements-dev.txt` 补齐共用文件。

真实压缩需要安装 Bandizip，并确保 `Bandizip.exe` 与 `bz.exe` 可在 PATH 中找到。应用正常运行调用 `bandizip`，自动集成测试调用控制台版 `bz`。Windows 凭据接口通过标准库 ctypes 调用，不依赖额外密码库。

VS Code 选择 `.venv\Scripts\python.exe` 解释器，打开 `main.py` 并使用 Python 扩展的“在终端中运行 Python 文件”。Windows PowerShell 不支持 `/usr/bin/env` 启动命令。

## 测试分层与隔离

默认回归测试不启动 GUI、不访问凭据、不要求安装 Bandizip，图形及真实压缩测试默认跳过：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

完整 Windows 集成测试：

```powershell
$env:SORTZIP_INTEGRATION = '1'
try {
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
} finally {
    Remove-Item Env:SORTZIP_INTEGRATION -ErrorAction SilentlyContinue
}
```

完整集成测试需要正常用户的 Windows 凭据访问权限；某些沙箱会拒绝真实凭据测试。测试使用随机目标 `SortZip/Test/<UUID>` 并清理，不读取应用的真实密码。GUI 使用独立 INI、内存凭据或模拟存储，并禁用/模拟更新检查。

自动化测试在独立临时目录生成素材，结束后清理。旧 `test/` 脚本的覆盖已迁入 `tests/`，对应关系见下表。手动验收和性能基准可用 `scripts/` 下的工具重新生成素材。完成检查后，可清理 `.build-validation/` 内生成的素材、缓存和临时 EXE。`preview-benchmark.json` 可保留用于历史性能对照。

| 旧脚本范围 | 可提交测试中的对应覆盖 |
|---|---|
| `test_resume.py`：正常、单次、二次、取消与恢复、后缀替换 | `test_safety.py`、`test_stage_two.py`、真实格式集成测试 |
| `test_resume.py`：源文件异常、断点排除、已完成任务 | 安全测试、执行计划测试及 `test_engineering.py` |
| `test_force.py`：进程强制取消、断点保留 | `test_engineering.py` 的真实 Python 子进程测试、安全测试的组取消测试 |
| `test_ui.py`：密码、映射、恢复导入、取消警告、线程释放及统计 | `test_gui_regressions.py`、`test_windows_integration.py` |

旧脚本中的模拟数据按当前有效断点结构重新生成，不照搬无效路径或全局修改 Qt 方法的写法。新增测试用局部 patch 和独立素材，避免污染其他用例。

## 代码职责与兼容入口

`layer_migration.migrate_legacy` 由主窗口用于统一层 UI 的偏好迁移。输入为已读取的普通配置字典，密码必须来自调用者已加载的凭据，不读取或写入明文设置。它将旧二次打包转换为“第一层旧分组、第二层每组 1 个完整单元”；`{source_range}` 模板保留原文件组序号命名，`{original}` 仍表示本层输入单元范围。中间层旧后缀伪装会返回 notices，不能静默带入新流程。首次使用配置映射为一层。

`layer_editor.LayerStack` 是直接嵌入压缩页的纵向卡片组件，配置在主窗口实时生效；不需要单独启用多层模式或打开编辑弹窗。旧控件仅作为内部兼容读写保留，不出现在文件/命名/压缩页的可见入口中。`unified_layers` 标记新偏好来源，`layer_format_preferences` 保留切换格式时的非秘密偏好；密码启用标记与实际密码分开，密码从不写入层偏好。

`nesting.py` 是 v0.9.0 开发中的多层计划模块，由 nested_pipeline 和 GUI 复用。`plan_layers` 生成完整单元之间的依赖，`resolve_inputs` 在执行时展开全部产物，拒绝未完成单元及 GZ/XZ 多实体输入；它不验证磁盘路径、文件存在性或断点落盘，执行器必须完成这些校验才可发布 `MaterializedUnit(complete=True)`。`Layer` 的密码不进入调试 repr，但不能将包含密码的 dataclass 直接序列化到清单或断点。

`nested_execution.execute_plan` 是独立开发入口，已使用真实 Bandizip 验证三层混合格式、分卷及往返解压。它保留输入及所有中间归档，状态采用 schema=2/kind=multilayer 的 `.sortzip_resume.json`；只保存密码是否需要，不保存密码。`nested_resume.resume_plan` 可校验并恢复此状态，密码按从 0 开始的层序号通过 `passwords` 提供；源文件在输出目录外时，调用者必须显式提供可信 `source_roots`，不能使用断点自行声明的白名单。未完成组的旧目录保留，新尝试写入 `.retry-<随机ID>` 目录。

主引擎 main_from_config 支持 compression_layers（Layer 或字段字典列表），由 nested_pipeline 复用分类/命名并建立跨层计划；没有该字段时继续使用旧压缩流程。resume_from 自动识别并恢复多层断点，密码通过 layer_passwords 提供。GUI 的 layer_editor 提供各层配置，恢复导入先校验结构和文件再收集各层密码。层设置保存时剔除密码，并另存密码需求标记，重启后阻止未经重新确认的加密层直接执行。原文件名脚本入包及最终清理仍待集成，最外层后缀还原包已自动输出。恢复器按大小/修改时间复核文件，不能识别刻意恢复相同元数据的内容修改。

多层示例配置（源码 API，与 GUI 使用同一主流程）：

```python
from sortzip_core.engine import main_from_config

main_from_config({
    'src': r'D:\input', 'dest': r'D:\output',
    'custom_names': {'.txt': 'doc'},
    'recursive': True, 'keep_hierarchy': True, 'keep_files': True,
    'output_list': True,
    'compression_layers': [
        {'group_size': 1, 'archive_format': '7z', 'enable_volume': True, 'volume': '100MB'},
        {'group_size': 2, 'archive_format': 'zip'},
        {'group_size': 2, 'archive_format': '7z', 'suffix': '.png'},
    ],
})
```

每层可设 name_template（归档基础名模板，默认 `{original}` 表示本层输入单元序号范围）、password、verify。仅最后一层允许 suffix；这里的 group_size 计数上一层单元而不是分卷文件。各分类子目录分别分组，List.txt 的归档列记录最终外层位置。当前保留全部中间产物，新断点成功后仍保留供核对。

| 模块 | 职责 |
|---|---|
| `planning.py` | 只读执行计划、排序、模板渲染与规则匹配，不依赖 Qt、不修改文件 |
| `config.py` | 共享密码规则和任务配置校验；`prepare_config` 返回带计划的配置副本 |
| `formats.py` | 真实格式能力及压缩参数校验 |
| `engine.py` | 文件执行、压缩进程、断点状态与统计；仍导出旧计划/命名函数供现有调用使用 |
| `tasks.py` | Worker、日志信号、取消标记、未启动线程工厂 |
| `app.py` | 工作台启动入口与更新检查；MainWindow 导出正式工作台 |
| `widgets.py` | 通用控件和弹窗；兼容导出 `Worker`、`LogStream` 和 `validate_password` |
| `ui_controls.py` | 保留 Qt 交互的复选框指示器绘制、数量与格式的响应式成组排版 |
| `credentials.py` | Windows 凭据管理和旧密码迁移 |
| `native_caption.py` | 主窗口专用 DWM 配色、主题及 HWND 重建同步；保留原生窗口框架 |

`main.py` 保持 GUI 启动入口；`SortZip.py` 保留历史入口，不是完整的命令行参数解析器。

`main_from_config` 和 `group_compress` 保持字符串结果：`completed`、`error`、`partial`、`safe_cancelled`、`force_cancelled`。可选 `stats` 字典提供执行计数。计划中的 `source` 是 `Path` 对象，供本地预览及执行使用；持久化恢复数据使用独立的 JSON 断点结构，不直接序列化整个计划。

`prepare_config(..., require_files=True)` 用于交互式执行，拒绝空任务、已存在的分类目录，以及递归模式下未保留层级的同分类重名任务；重复名称按 Windows 大小写规则识别，要求用户开启层级。引擎调用保留空任务返回成功、已有目录中无目标占用及扁平名称避让的兼容行为。确认后的计划由引擎在首次操作前检查源文件元数据。

`MainWindow` 支持可选 `settings`、`credential_store` 和 `check_updates` 参数，默认行为不变。测试可注入独立设置及存储，避免接触真实用户数据。线程工厂返回尚未启动的线程与 Worker，界面连接信号后再启动；线程退出后由界面清理引用。

## 源码及打包启动验证

`restore_bundle.write_bundle` 生成目录式 CMD/PowerShell/JSON 还原包，读取相邻父目录中的指定文件，只有文件基名进入映射。脚本两阶段暂存/提交，原子记录 Restore-State.json，支持部分阶段继续和重复运行。构建脚本将 restore_names.ps1 作为包数据收集，生成器通过模块旁路径读取；不能省略此资源。多层桥接在压缩成功后为最外层后缀修改生成还原包，失败保留产物及断点，可恢复重试。当前原文件名脚本尚未自动嵌入最内层归档，后续仍需元数据携带、单流格式兼容和清理集成。

`test_review_regressions.py` 覆盖本次复审的断点保护、修改时间复核、恢复保留文件、参数预检和目录扫描次数；GUI 回归覆盖重复启动、连续任务、关闭等待及线程创建失败后的控件恢复。

真实窗口验收可运行 `.\.venv\Scripts\python.exe scripts/acceptance_gui.py`。脚本在 `.build-validation/acceptance/` 生成独立素材、INI 和结果 JSON，使用内存凭据并关闭更新检查。可加 `--slow-compression --compression-delay 20`，仅在此测试宿主中给真实压缩命令加等待，以便操作运行中关闭及恢复；`--preview-delay 120` 为预览线程加入可取消等待，用于关闭生命周期检查。生产程序不含这些延迟。`--compress --volume` 验证真实两层分卷；大文件强制中断使用 `--compress --payload-kib 131072 --first-format 7z --group-size 12`，确认 process-events.jsonl 中真实进程已启动并出现 ForceCancelled，不能用等待期间的取消代替。测试窗口中的操作仍须人工或桌面自动化完成，脚本启动本身不代表通过验收。

提供独立启动检查，不显示正常主窗口、不读真实设置或密码、不请求更新：

```powershell
New-Item -ItemType Directory -Force .build-validation\tmp | Out-Null
$env:TEMP = (Resolve-Path .build-validation\tmp).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe main.py --smoke-test .build-validation\source-smoke.json
Get-Content .build-validation\source-smoke.json
```

成功报告包含 `status: ok`、版本、4 个共享编辑页、单层初始状态、真实后台预览及可用格式。该入口使用临时 INI 和内存凭据，检查窗口、图标和执行计划加载；不能替代文件处理及完整 GUI 回归。

构建发布用 EXE：

```powershell
.\scripts\build.ps1
```

`scripts/build.ps1` 从项目根目录调用 `SortZip.spec`，使用项目版本生成 `release/SortZip-v<版本>.exe` 和对应的 `.sha256`。冻结启动检查通过后才替换同版本成品。报告保存在 `.build-validation/`，临时 EXE 和构建缓存会自动清理。默认使用 `.venv`，也可通过 `-Python` 指定解释器。脚本使用 [PyInstaller](https://pyinstaller.org/en/stable/)打包应用图标和后缀还原脚本，构建时临时收敛 PATH，避免混入其他软件的旧运行库。

验证冻结后的启动：

```powershell
$report = Join-Path (Get-Location) '.build-validation\frozen-smoke.json'
$process = Start-Process -FilePath '.\release\SortZip-v1.0.0.exe' `
    -ArgumentList @('--smoke-test', ('"' + $report + '"')) `
    -WindowStyle Hidden -Wait -PassThru
$process.ExitCode
Get-Content $report
```

干净环境验收时复制同一份候选 EXE 和校验文件，用 `Get-FileHash -Algorithm SHA256` 确认一致。在该环境中设置 `SORTZIP_SMOKE_TOOL` 为 `missing`、`bandizip`、`7zip` 或 `winrar`。多工具场景把 `SORTZIP_SMOKE_DETECTED` 设为以逗号分隔的工具名，例如 `bandizip,7zip,winrar`，并通过 `SORTZIP_SMOKE_TOOL` 选择本次使用的工具。检查包含旧偏好迁移、分类取消恢复、旧多层断点恢复、后缀还原脚本、分卷逐层解包及 `List.txt` 入口核对。最终发布使用通过验收的同一份产物。

将 `release/` 中验收通过的 EXE 和校验文件作为附件上传到 GitHub Releases。应用和打包共用 `assets/sortzip.ico`，README 使用 `docs/images/sortzip.svg`。

检查退出码为 0、报告状态为 `ok`。本地环境及临时构建产物均已忽略；验证成功不表示已发布新版本。正常发布前仍需维护 README、CHANGELOG 与内置使用手册，并核对版本对应的实际修改。

层卡片组件回归位于 `tests/test_ui_controls.py`，覆盖方框/标签/空格切换、禁用状态、指示器与原生标签绘制、联动及偏好保存、滚动重绘、标题更新和紧凑字段换行。可分别设置 `QT_SCALE_FACTOR=1.5`、`2` 检查高 DPI 逻辑与排版。离屏绘制通过不能替代 Windows 浅色原生窗口验收；发布前仍须在 640×600、900×760 和高 DPI 环境实际操作分卷、密码、校验、后缀及展开区域，确认勾号持续可见。

## 原生标题栏融合与标识隐藏（v0.9.2）

主窗口由 native_caption.py 封装 DWM 配色和 UxTheme 非客户区属性。Win11 标题背景与品牌区同步；Win10 只尝试深色模式。不支持的属性记录日志并保留系统外观，不修改系统全局主题。

按最新布局要求，使用 WTNCA_NODRAWCAPTION / NODRAWICON、WS_EX_DLGMODALFRAME 装饰及仅清除 ICON_SMALL 隐藏重复标识。窗口名称仍为 SortZip，ICON_BIG 和 Qt 应用图标保留，不更改 WS_THICKFRAME、系统菜单、APPWINDOW / TOOLWINDOW 或客户端命中。WS_EX_DLGMODALFRAME 不设置窗口模态；它只用于原生边框装饰。主题切换、图标变化及 HWND 重建重新应用。不使用无边框窗口、文字同色伪装或绘制钩子。品牌行顶部内边距从 8px 降至 0；其他布局不改。

接口依据 Microsoft 的 WTNCA、SetWindowThemeAttribute 及 Extended Window Styles 文档。SetWindowLongW 仅操作32位扩展样式，HWND 使用指针宽度；检查 HRESULT、LastError 及 SetWindowPos 返回值。若系统不接受隐藏属性则保留可识别的原生标识。

验收宿主 --native-caption 保留生产标题并记录主题与 HWND；仅宿主提供 Ctrl+Alt+T（原外观 QAction）、Ctrl+Alt+R（重建原生句柄），生产程序没有这些快捷键。小报告保存在被忽略的 .build-validation，生成素材、临时 EXE 与构建缓存验收后清理。

## 密码可见性图标（v0.9.2）

各层共用 PasswordVisibility（QAction），同时附加到密码与确认密码框的 TrailingPosition；Qt负责右端按钮空间，取消原独立选框。保留toggled/isChecked接口、按层草稿和显示状态仅会话内的规则。向量图标按调色板及DPI绘制，附带显示／隐藏密码的悬停说明与无障碍名称，支持Tab、空格；不改密码内容及校验。

279项完整回归通过，新增检查双输入同步、单次信号、鼠标／键盘、右侧位置与格式禁用。Windows原生150%浅／深窗口点击均通过；全DPI矩阵未重测。编译、源码与临时EXE启动结果见本轮password-eye报告；构建及验收素材用完清理。

## 空密码即不加密（v0.9.2）

移除 LayerRow 的密码启用复选框，不保留隐藏控制路径。密码框始终在支持格式下可直接编辑，非空密码须至少8位且两次一致；主密码为空时不加密，确认框残留不触发密码校验。格式不支持密码时两个输入及眼睛禁用，切回支持格式保留会话输入。第一层向后应用直接依据有效密码。

工作区导出以密码内容为准，旧 encrypt / encrypted / layer_password_required 字段仍可读取，但不再强迫空密码层加密。密码仍不写偏好、日志或新断点；重新启动后为空，需加密必须重新填写。旧加密断点恢复仍单独收集原密码，不用空输入覆盖原任务的加密要求。

281项完整回归通过，新增空密码／确认残留、旧启用标志与密码内容不一致、密码校验及无勾选项提示检查；其余界面测试改用直接输入，保留格式锁定、层草稿、密码应用和真实执行断言。原生150%浅色已核对占位文字、右侧眼睛和布局；未重跑全部DPI／主题交叉矩阵。启动与编译结果见本轮 blank-password 报告。

## 按任务选择压缩工具

正式多层任务使用 archive_tools.py 生成各工具的命令、识别入口与分卷并核对能力。命令参数以列表调用，7-Zip 与 Rar.exe 使用仅含路径的 UTF-8 临时文件列表。工具可用性在只读预检及执行开始时复核。分卷交给下一层前须连续，7-Zip 与 Rar.exe 的分卷还会做完整性校验。参数及子进程输出中的密码统一脱敏。

新 schema 3 断点记录工具标识和是否需要文件清单。schema 2 及更早的断点走 Bandizip 兼容路径。程序路径只由本机设置或 PATH／安装目录决定。新任务全部归档后按实际入口卷写入 List.txt，恢复后也可以补写。相同清单可重试，已有不同清单会报告占用并保留断点。旧断点保留原有清单。

正式工作台当前写入 schema 4。`task_recovery` 在首次文件操作前固定文件及归档计划，由 `file_journal` 在同一输出目录持有任务锁并使用 SQLite 记录操作意图和完成状态。Windows 同盘改名使用不覆盖的 `MoveFileExW`，复制与跨盘移动使用同目录临时文件，核对后提交。跨盘源文件待目标提交并记账后才删除。恢复仍需处理原位置时，调用者应在用户确认后传入 `approved_input_root`。原有 schema 2／3 和早期断点继续由兼容路径读取。

`resume_passwords` 使用独立随机盐和固定参数的 scrypt 核验待压缩层。持有断点即可离线猜测密码。旧多层断点优先用同层已完成归档验证密码，改后缀的卷在临时副本中还原标准卷名。无样本的层由界面提示。任务与恢复确认前在后台检测工具，工作进程在文件操作前复核锁定程序的身份。独立 Windows 环境的发布候选矩阵仍是发布门槛。

本机真实验收使用 Bandizip 7.46、7-Zip 26.01 和 RAR 7.23。可通过 SORTZIP_TEST_7ZIP 和 SORTZIP_TEST_WINRAR 指定集成测试用的绝对路径。289 项完整回归通过。验证中文及空格路径、最长测试清单 603 个输入、带空格及特殊符号的密码、ZIP／7Z／RAR5 分卷、缺卷、安全／强制取消、独立进程恢复、改后缀还原及逐字节核对。7-Zip 的 ZIP 密码要求可打印 ASCII，7-Zip／RAR 命令行双引号密码在预检时拒绝。小样本耗时与体积记录在 multi-tool-benchmark.json。

冻结 EXE 在同一台 Windows 的进程隔离环境验收。清空可见工具 PATH 后分别测试无工具、仅 Bandizip、仅 7-Zip、两者共存和仅 Rar.exe。五种环境均通过单包工作流或缺工具无写入检查。此矩阵尚未在独立 Windows 虚拟机重跑。工具需要自行安装或配置程序路径，临时验收工具不打入 SortZip EXE。
