# <img src="docs/images/sortzip.svg" width="40" height="40" alt=""> SortZip

**简体中文** · [English](README.md)

SortZip 是一个 Windows 文件整理工具。按扩展名分目录，按规则批量改名，在同一个工作台预览结果，再按需分组压缩。适合整理下载目录、归档项目资料，或把一批文件分成便于交付的多个压缩包。

[下载 SortZip](https://github.com/WindySumm/SortZip/releases) · [版本记录](CHANGELOG.md) · [开发与构建](DEVELOPMENT.md)

![SortZip 工作台示例。左侧列出分类、命名和两层压缩，中间编辑命名规则，右侧实时对照原文件名和新文件名。](docs/images/workbench.png)

## 下载与运行

在 GitHub Releases 选择所需版本，下载并运行对应的 EXE，例如 `SortZip-v1.0.1.exe`。分类和重命名可直接使用。需要压缩时，安装下列任一工具，然后在「最终输出」选择。程序路径可在右上角菜单的「设置」中指定并检测。

首次启动按系统首选显示语言选择简体中文或英语，其他语言使用英语。也可以在「设置 → 界面语言」手动选择，重启 SortZip 后生效。

| 压缩工具 | SortZip 支持的格式 | 支持加密与分卷的格式 |
| --- | --- | --- |
| [Bandizip](https://www.bandisoft.com/bandizip/) · `bz.exe` | ZIP、7Z、TAR、LZH、GZ、XZ | ZIP、7Z |
| [7-Zip](https://www.7-zip.org/) · `7z.exe` | ZIP、7Z | ZIP、7Z |
| [WinRAR](https://www.rarlab.com/) · `Rar.exe` | RAR5 | RAR5 |

一个任务的所有压缩层共用同一工具，各层可以选用该工具支持的不同格式。GZ 和 XZ 每包只能包含一个实体文件。发布页若附有 `.sha256` 文件，可在下载目录运行 `Get-FileHash .\SortZip-v1.0.1.exe -Algorithm SHA256`，将结果与校验文件比对。

## 第一次使用

1. **选文件夹。** 填写输入和输出目录。需要处理下级目录时，勾选「包含子文件夹」。
2. **做分类。** 在「分类映射」把扩展名对应到目录，例如把 `.txt` 放入「文档」，把 `.png` 放入「图片」。
3. **预览新名称。** 在「原文件命名」选择排序方式、设置命名规则。比如给「文档」使用 `文档_{n}{ext}`，右侧会实时显示原文件名和新名称。模板留空时保留原名。
4. **配置压缩。** 在「第 1 层」填写每包文件数、格式和归档名称。需要再打包一层时，点击流程标题旁的「＋」。只想分类和改名时，关闭「启用压缩」。
5. **核对后执行。** 在「最终输出」查看产物，按需勾选「输出文件对应清单」。初次试用建议勾选「保留原始文件」，这样 SortZip 会复制源文件。关闭这个选项时，分类会移动源文件。核对预览后点击「开始执行」。

## 多层打包与分卷

第一层按文件分组。第二层起，每一包按上一层的**完整归档单元**计数。比如第一层生成 A、B、C 三套分卷，第二层每包 2 个单元，结果就是「A 的全部卷＋B 的全部卷」和「C 的全部卷」。

各层可独立设置密码、分卷大小和完整性校验，具体选项随格式而定。密码留空时该层不加密，填写时至少 8 位并输入两次。7-Zip 加密 ZIP 时使用可打印 ASCII 字符，7-Zip 和 WinRAR 的密码避免双引号。层密码保留在当前会话。

只有最外层可以更改显示后缀。比如把 ZIP 显示为 `.data`，归档内容仍是 ZIP。需要交付时，把整套归档和 `Restore-Archive-Names` 文件夹一起复制。在交付副本中运行 `Restore-Names.cmd`，即可改回标准归档名称。

## 输出与任务恢复

- **对应清单。** 勾选「输出文件对应清单」后，SortZip 在各分类目录生成 `List.txt`，列出原相对路径和新文件名。压缩完成后，还会列出所属最外层归档的实际入口卷和标准卷名。文件以制表符分隔，可导入 Excel。
- **中断后继续。** 保留输出目录和其中的 `.sortzip_resume.json`、`.sortzip-files.sqlite3`，在底部选择「任务 → 恢复任务」。恢复时会复用已完成的步骤，并在需要时询问原输入目录和所属压缩层的密码。
- **保管断点。** 加密任务的断点含密码校验数据，持有者可离线尝试猜测密码。交付时只需复制所需的归档、清单和后缀还原文件。
- **输出空间。** SortZip 会保留分类后的文件和中间归档。开始不同的任务时，使用新的输出目录。

更多操作说明可在 SortZip 的右上角菜单打开「使用手册」。

## 从源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

测试、打包和模块说明见 [DEVELOPMENT.md](DEVELOPMENT.md)。

项目采用 MIT 许可。
