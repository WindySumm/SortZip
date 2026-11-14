# <img src="docs/images/sortzip.svg" width="40" height="40" alt=""> SortZip

[简体中文](README.zh-CN.md) · **English**

SortZip is a Windows utility for organizing files. Sort files into folders by extension, rename them in batches, preview the result, and optionally package them into archives. Use it to organize a downloads folder, archive project files, or split a delivery into several archives.

[Download SortZip](https://github.com/WindySumm/SortZip/releases) · [Changelog (Chinese)](CHANGELOG.md) · [Development and build guide (Chinese)](DEVELOPMENT.md)

![SortZip workbench with the workflow on the left, naming rules in the center, and a live filename preview on the right. The example uses the Chinese interface.](docs/images/workbench.png)

## Download and run

Choose a version from GitHub Releases and run its EXE, such as `SortZip-v1.2.5.exe`. You can sort and rename files without installing a compression tool. To compress files, install one of the tools below and select it under **Final output**. Use **Settings** in the top-right menu to set and test its executable path.

On first launch, SortZip follows your system's preferred display languages. It supports Simplified Chinese and English, with English as the fallback. To override this, choose **Settings → Language** and restart SortZip.

| Tool | Formats | Passwords and split volumes |
| --- | --- | --- |
| [Bandizip](https://www.bandisoft.com/bandizip/) · `bz.exe` | ZIP, 7Z, TAR, LZH, GZ, XZ | ZIP, 7Z |
| [7-Zip](https://www.7-zip.org/) · `7z.exe` | ZIP, 7Z | ZIP, 7Z |
| [WinRAR](https://www.rarlab.com/) · `Rar.exe` | RAR5 | RAR5 |

All layers in a task use the same tool. Each layer can use a different format supported by that tool. A GZ or XZ archive can contain one file.

## First task

1. **Choose folders.** Set the input and output folders. Enable **Include subfolders** when needed.
2. **Map file types.** Under **File type mappings**, map an extension to a folder, such as `.txt` to `Documents`.
3. **Preview names.** Under **Rename files**, set the order and add rules. For example, `Document_{n}{ext}` produces numbered names. A blank template keeps the original name.
4. **Set up compression.** Under **Layer 1**, choose the files per archive, format, and archive naming template. Use **＋** next to the workflow heading to add another layer. Turn off **Enable compression** for a classification and renaming task.
5. **Review and run.** Under **Final output**, choose which files and archive layers to keep. A new compression profile keeps only the outermost layer by default. Select **Keep original files in the input folder** to copy source files. If you leave it off, SortZip moves them.

## Nested archives and split volumes

The first layer groups individual files. In later layers, each archive from the previous layer counts as one input, even if it spans several volumes. For example, if the first layer produces split archives A, B, and C, a group size of two puts all volumes of A and B in one archive and all volumes of C in another.

Password protection, split volumes, and verification depend on the archive format. Leave the password blank to skip encryption, or enter at least eight characters twice. 7-Zip requires printable ASCII passwords for ZIP. Avoid double quotes when using 7-Zip or WinRAR. Passwords are kept for the current session.

You can change the filename extension of the outermost archive. A ZIP shown as `.data` still contains ZIP data. Include the `Restore-Archive-Names` folder when delivering the complete set. Run `Restore-Names.cmd` on the delivery copy to restore standard archive names.

## Output and recovery

- **File list.** **Write file list** is off by default. When enabled, SortZip writes a UTF‑8, tab-separated `List.txt` in each category under the output folder. It records original relative paths and new filenames. For compressed tasks, it also records the actual outermost entry volume, its standard filename, and the number of archive files. The result shows how many lists are available, including any reused after resuming. Review each list before sharing it because relative paths can reveal original filenames and subfolder names. SortZip leaves the list in the output folder.
- **Resume.** Keep the output folder, `.sortzip_resume.json`, and `.sortzip-files.sqlite3` together. Choose **Task → Resume task** or drop the checkpoint on the Task button. SortZip reuses completed steps and may ask for the original input folder or passwords.
- **Protect checkpoints.** An encrypted task's checkpoint stores password verification data that allows offline guessing attempts. Keep checkpoints private when sharing archives.
- **Disk space.** The confirmation shows free space, estimated writes, and the stage with the highest estimated space usage on each volume. It also shows the largest inner layers you chose to keep. SortZip stops if a copy cannot fit. You can proceed if only the compression estimate is tight, or resume after freeing space.
- **Cleanup.** SortZip removes working files and unselected inner archives only after the next layer passes its integrity and member checks and the checkpoint is saved. A missing file or volume stops cleanup for that group. Classification-only tasks keep the classified files. When resuming an older checkpoint, SortZip keeps any inputs still present for groups without a recorded member check.
- **Restore original filenames.** Enable **Include original filename restoration scripts** under **Final output** when you want recipients to see the original filenames. Extract each layer manually and give each innermost archive its own folder. Run `SortZip-Restore-Names.cmd` there. It verifies files and reports the path of any conflict before proceeding. If interrupted, keep the extracted files, temporary files and restore files together, then run the same command again. GZ and XZ cannot include the script.

Open **User Guide** from the top-right menu for more details.

## Unpack nested SortZip archives

Under **Final output → SortZip Unpack and Restore**, enable **Create automatic unpack manifest** and share each complete outer volume set with its `.sortzip.json`. This workflow supports ZIP, 7Z, and RAR5 deliveries and requires the corresponding command-line tool on the recipient's computer. The external manifest reveals the outer format and standard volume names. **Automatically restore original filenames** stores the original filenames in the innermost archive. To keep those names private, also leave the standalone rename script disabled and review any `List.txt` before sharing it.

Open **Task → Unpack and restore**. In the dialog, browse for one or more `.sortzip.json` manifests and an output folder, then select **Start unpacking**. Keep each manifest beside its complete set of archive files. SortZip reads original names from the innermost archive. Files keep their current names if originals are unavailable or you turn off **Restore original filenames when available**. Each delivery and each innermost group gets its own folder. You can retain extracted inner archives. The delivered volumes stay in place. To resume, choose the same manifests and output folder and re-enter passwords.

Share passwords separately. SortZip tries the task's password list in order. If 7-Zip reports that a password may be wrong, SortZip tries the remaining entries. If none work, it explains that the archive may be damaged and lets you enter another password or cancel and check the archive. Confirmed missing volumes and checksum mismatches stop the task. Passwords are kept in task memory. Software with access to local process arguments may see a password while an archive tool runs.

## Run from source

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for tests and packaging. SortZip uses the MIT license.
