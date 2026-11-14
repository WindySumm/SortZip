MANUAL_TEXT_EN = """<h2>User Guide</h2>
<p>SortZip v1.0.0</p>
<h3>Set up a task</h3>
<ol>
<li>Choose an input and an output folder. Enable <b>Include subfolders</b> to scan nested folders.</li>
<li>Under <b>File type mappings</b>, enable the extensions you want to process and choose their category folders. Use <b>Map all</b> to include all scanned types.</li>
<li>Under <b>Rename files</b>, choose an order and edit naming rules. The live preview compares original and new names. Enable <b>More details</b> to also see category and archive locations. Refresh the preview after changing files outside SortZip.</li>
<li>Set up the compression layers or turn off <b>Enable compression</b>. Review <b>Final output</b> before clicking <b>Start task</b>.</li>
</ol>
<p><b>Keep original files</b> copies the source files. If you leave this option off, SortZip moves the source files. Output folders with a saved task must be resumed or replaced with another destination.</p>
<h3>Naming and order</h3>
<p>Rules match category folders from top to bottom. With <b>Keep folder structure</b>, subfolders can inherit their parent's rule. A blank template keeps original names. Use <code>{n}</code> for the file number, <code>{original}</code> for the original base name, <code>{ext}</code> for its extension including the dot, and <code>{folder}</code> for the folder name.</p>
<p>You can reorder mappings and rules, and drag files within one preview category. Undo and redo apply to configuration changes during this session. To keep files with the same name in separate folders, enable <b>Keep folder structure</b>.</p>
<h3>Compression</h3>
<p>Choose one tool per task in <b>Final output</b>. Bandizip supports ZIP, 7Z, TAR, LZH, GZ and XZ. 7-Zip supports ZIP and 7Z, and WinRAR's console tool supports RAR5. GZ and XZ accept a single physical file. Use <b>Settings</b> to specify or detect the command-line program.</p>
<p>Use <b>＋</b> next to the workflow heading to add a layer. The first layer counts files, and later layers count complete archive units. All volumes of a split archive stay together. The archive naming template uses <code>{n}</code> for the group number, <code>{original}</code> for the range of inputs in that layer, <code>{source_range}</code> for the original file number range, and <code>{folder}</code> for the folder name.</p>
<p>Where supported, enter a volume size such as <code>100MB</code> or leave it blank for automatic sizing. Enable verification to test archives after creation. Leave the password blank for no encryption. Otherwise enter at least eight characters twice. ZIP passwords in 7-Zip require printable ASCII, and 7-Zip and WinRAR passwords cannot contain double quotes. Passwords are kept for this session. The eye icon shows or hides the password. Use Apply password to later layers to reuse the confirmed password from the first layer.</p>
<p>You can change the filename extension of the outermost archive. To restore standard archive names, copy all volumes and the <code>Restore-Archive-Names</code> folder together and run <code>Restore-Names.cmd</code> on the copy. The script renames archive volumes. It does not extract them or restore the original names of files inside them.</p>
<h3>Cancel and resume</h3>
<p>Safe cancellation waits for a recoverable operation to finish. Force cancellation can stop an active compression process, and the unfinished archive may need to be created again. Closing the window waits for safe cancellation and worker shutdown.</p>
<p>Keep <code>.sortzip_resume.json</code> and <code>.sortzip-files.sqlite3</code> with the output. Choose <b>Task → Resume task</b> or drag the checkpoint onto the Task button. SortZip reuses finished steps and may ask you to confirm the input folder or enter original passwords. Avoid moving or editing files in an unfinished task. A checkpoint for an encrypted task contains password verification data that can be used to guess passwords offline.</p>
<p>With <b>Write file list</b> enabled, each category receives a tab-separated <code>List.txt</code>. It has original paths, new names and, for compressed tasks, the actual and standard outermost entry volumes. File list column names are kept stable across interface languages. Classified files and intermediate archives remain in the output.</p>
<h3>Settings</h3>
<p>Use the top-right menu for appearance, settings and this guide. Choose <b>Language</b> in Settings, then restart SortZip. The <b>System</b> setting follows the preferred display languages, with English as the fallback. Use the task log to check progress and errors.</p>
"""
