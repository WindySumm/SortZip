# <img src="docs/images/sortzip.svg" width="40" height="40" alt=""> SortZip

[简体中文](README.zh-CN.md) · **English**

SortZip is a Windows utility for organizing files. Sort files into folders by extension, rename them in batches, preview the result, and optionally package them into archives. Use it to organize a downloads folder, archive project files, or split a delivery into several archives.

[Download SortZip](https://github.com/WindySumm/SortZip/releases) · [Changelog (Chinese)](CHANGELOG.md) · [Development and build guide (Chinese)](DEVELOPMENT.md)

![SortZip workbench with the workflow on the left, naming rules in the center, and a live filename preview on the right. The example uses the Chinese interface.](docs/images/workbench.png)

## Download and run

Choose a version from GitHub Releases and run its EXE, such as `SortZip-v1.0.0.exe`. You can sort and rename files without installing a compression tool. To compress files, install one of the tools below and select it under **Final output**. Use **Settings** in the top-right menu to set and test its executable path.

On first launch, SortZip follows your system's preferred display languages. It supports Simplified Chinese and English, with English as the fallback. To override this, choose **Settings → Language** and restart SortZip.

| Tool | Formats | Passwords and split volumes |
| --- | --- | --- |
| [Bandizip](https://www.bandisoft.com/bandizip/) · `bz.exe` | ZIP, 7Z, TAR, LZH, GZ, XZ | ZIP, 7Z |
| [7-Zip](https://www.7-zip.org/) · `7z.exe` | ZIP, 7Z | ZIP, 7Z |
| [WinRAR](https://www.rarlab.com/) · `Rar.exe` | RAR5 | RAR5 |

All layers in a task use the same tool. Each layer can use a different format supported by that tool. A GZ or XZ archive can contain one file. If the release includes a checksum, run `Get-FileHash .\SortZip-v1.0.0.exe -Algorithm SHA256` in the download folder and compare the result with the `.sha256` file.

## First task

1. **Choose folders.** Set the input and output folders. Enable **Include subfolders** when needed.
2. **Map file types.** Under **File type mappings**, map an extension to a folder, such as `.txt` to `Documents`.
3. **Preview names.** Under **Rename files**, set the order and add rules. For example, `Document_{n}{ext}` produces numbered names. A blank template keeps the original name.
4. **Set up compression.** Under **Layer 1**, choose the files per archive, format, and archive naming template. Use **＋** next to the workflow heading to add another layer. Turn off **Enable compression** for a classification and renaming task.
5. **Review and run.** Check **Final output** and optionally enable **Write file list**. Select **Keep original files** to copy source files. If you leave this option off, SortZip moves the source files. Click **Start task** when ready.

## Nested archives and split volumes

The first layer groups individual files. In later layers, each archive from the previous layer counts as one input, even if it spans several volumes. For example, if the first layer produces split archives A, B, and C, a group size of two puts all volumes of A and B in one archive and all volumes of C in another.

Password protection, split volumes, and verification depend on the archive format. Leave the password blank to skip encryption, or enter at least eight characters twice. 7-Zip requires printable ASCII passwords for ZIP. Avoid double quotes when using 7-Zip or WinRAR. Passwords are kept for the current session.

You can change the filename extension of the outermost archive. A ZIP shown as `.data` still contains ZIP data. Include the `Restore-Archive-Names` folder when delivering the complete set. Run `Restore-Names.cmd` on the delivery copy to restore standard archive names.

## Output and recovery

- **File list.** With **Write file list** enabled, categories receive a tab-separated `List.txt` that can be opened in Excel. It records original relative paths and new names, plus the name of the volume to open and its standard name when compression completes.
- **Resume.** Keep the output folder, `.sortzip_resume.json`, and `.sortzip-files.sqlite3` together. Choose **Task → Resume task** or drop the checkpoint on the Task button. SortZip reuses completed steps and may ask for the original input folder or passwords.
- **Protect checkpoints.** An encrypted task's checkpoint stores password verification data that allows offline guessing attempts. Keep checkpoints private when sharing archives.
- **Output space.** SortZip keeps classified files and intermediate archives. Use a separate output folder when starting another task.

Open **User Guide** from the top-right menu for more details.

## Run from source

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for tests and packaging. SortZip uses the MIT license.
