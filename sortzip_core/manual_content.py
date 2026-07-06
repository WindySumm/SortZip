MANUAL_TEXT = """<h2>使用手册</h2>

<hr>

<h3>工作流程概述</h3>
<ol>
  <li><b>设置源与目标</b> — 在「文件」页面选择源文件夹和目标根目录</li>
  <li><b>选择扩展名</b> — 在「映射」页面勾选需要处理的文件类型，可自定义文件夹名</li>
  <li><b>配置命名规则</b>（可选）— 在「命名」页面为每个文件夹单独设置重命名模板</li>
  <li><b>压缩选项</b> — 在「开始」页面选择压缩格式，点击执行</li>
  <li><b>查看结果</b> — 完成后弹出统计报告，可点击「打开目标文件夹」跳转</li>
</ol>
<p><b>执行前自动校验：</b>目标文件夹冲突、源文件类型匹配、命名模板重复检测。</p>

<hr>

<h3>导航页说明</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
<tr><th>页面</th><th>主要功能</th></tr>
<tr><td><b>文件</b></td><td>源文件夹、目标目录、每组文件数、排序方式（文件名/修改时间）、压缩密码、分卷设置（留空自动）</td></tr>
<tr><td><b>映射</b></td><td>按类型分组勾选扩展名（视频/音乐/图片/文档/程序/字体/压缩包），支持自定义文件夹名</td></tr>
<tr><td><b>命名</b></td><td>每个文件夹独立命名模板 + 预设快速填充；压缩后重命名（启用后缀替换）</td></tr>
<tr><td><b>预览</b></td><td>标签页切换文件夹，查看所有文件重命名前后对比</td></tr>
<tr><td><b>设置</b></td><td>保留原始文件、二次压缩、自动关闭 Bandizip、深色模式切换、版本信息</td></tr>
<tr><td><b>开始</b></td><td>选择压缩格式、执行/取消、实时进度条与日志、完成后统计报告（含打开目标文件夹）</td></tr>
</table>

<hr>

<h3>命名模板说明</h3>
<p>在「命名」页面中，可为每个分类文件夹单独设置命名规则。</p>
<ul>
  <li><b>模板留空</b> ＝ 不进行重命名，保留原始文件名</li>
  <li><b>模板非空</b> ＝ 按照模板中的占位符生成新文件名</li>
  <li>每条规则可以单独启用/禁用</li>
  <li>匹配文件夹为 <code>*</code> 时，匹配所有未被其他规则匹配的文件夹</li>
</ul>

<h4>可用占位符</h4>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
<tr><th>占位符</th><th>说明</th><th>示例值</th></tr>
<tr><td><code>{n}</code></td><td>序号（从 1 开始递增）</td><td><code>1</code>、<code>2</code>、<code>3</code></td></tr>
<tr><td><code>{ext}</code></td><td>文件扩展名（含小数点）</td><td><code>.jpg</code>、<code>.docx</code></td></tr>
<tr><td><code>{folder}</code></td><td>文件所在的文件夹名</td><td><code>照片</code>、<code>文档</code></td></tr>
<tr><td><code>{original}</code></td><td>原始文件名（不含扩展名）</td><td><code>IMG_001</code>、<code>photo_2024</code></td></tr>
</table>

<h4>预设模板示例</h4>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
<tr><th>模板</th><th>原始文件名</th><th>重命名后</th></tr>
<tr><td><code>{n}{ext}</code></td><td>IMG_001.jpg</td><td>1.jpg</td></tr>
<tr><td><code>{folder}_{n}{ext}</code></td><td>IMG_001.jpg</td><td>照片_1.jpg</td></tr>
<tr><td><code>{original}{ext}</code></td><td>IMG_001.jpg</td><td>IMG_001.jpg</td></tr>
<tr><td><code>{original}_{n}{ext}</code></td><td>IMG_001.jpg</td><td>IMG_001_1.jpg</td></tr>
<tr><td><code>{folder}_{original}{ext}</code></td><td>IMG_001.jpg</td><td>照片_IMG_001.jpg</td></tr>
</table>

<hr>

<h3>输出结构示例</h3>
<pre>
E:\\输出文件夹\\
├── 图片\\
│   ├── 照片_1.jpg
│   ├── 1-4.zip
│   └── 1-4.zipp
├── 文档\\
│   ├── 1.txt
│   └── 1-2.zipp
└── 视频\\
    └── ...
</pre>
<p>压缩包名始终基于组序号（如 <code>1-4.zipp</code>），不受文件命名规则影响。</p>

<hr>

<h3>注意事项</h3>
<ul>
  <li>本工具<b>仅支持 Bandizip</b>，必须安装并加入 PATH</li>
  <li>密码非空时 ≥ 8 位</li>
  <li>模板留空 = 不重命名，保留原始文件名</li>
  <li>扩展名映射默认不勾选，需手动选择</li>
  <li>勾选「保留原始文件」时源文件仅复制，压缩后副本自动清理</li>
  <li>首次使用建议先小范围测试</li>
</ul>
"""
