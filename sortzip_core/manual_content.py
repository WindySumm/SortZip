MANUAL_TEXT = """<h2>使用手册</h2>

<hr>

<h3>工作流程概述</h3>
<ol>
  <li><b>设置源与目标</b> — 在「文件」页面选择源文件夹和目标根目录</li>
  <li><b>选择扩展名</b> — 在「映射」页面勾选需要处理的文件类型，可自定义文件夹名</li>
  <li><b>配置命名规则</b>（可选）— 在「命名」页面为每个文件夹单独设置重命名模板</li>
  <li><b>压缩选项</b> — 在「开始」页面选择压缩模式与格式，点击执行</li>
  <li><b>查看结果</b> — 完成后弹出统计报告，可点击「打开目标文件夹」跳转</li>
</ol>
<p><b>执行前自动校验：</b>目标文件夹冲突、源文件类型匹配、命名模板重复检测。</p>

<hr>

<h3>导航页说明</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
<tr><th>页面</th><th>主要功能</th></tr>
<tr><td><b>文件</b></td><td><b>文件选择：</b>源文件夹、目标目录、包含子文件夹；<b>密码设置：</b>压缩密码、确认密码、记住密码；<b>分卷设置：</b>每包文件数、分卷大小、启用分卷</td></tr>
<tr><td><b>映射</b></td><td>按类型分组勾选扩展名（视频/音乐/图片/文档/程序/字体/压缩包），支持自定义文件夹名</td></tr>
<tr><td><b>命名</b></td><td><b>排序设置：</b>7 种排序预设（文件名升降序、修改时间、文件大小、扩展名）；每个文件夹独立命名模板 + 预设快速填充；压缩后重命名（启用后缀替换）</td></tr>
<tr><td><b>预览</b></td><td>标签页切换文件夹，查看所有文件重命名前后对比</td></tr>
<tr><td><b>设置</b></td><td>深色模式切换、使用手册、版本信息</td></tr>
<tr><td><b>开始</b></td><td><b>压缩设置：</b>一次压缩、二次压缩、压缩格式；<b>其他选项：</b>保留原始文件、输出目录（生成 List.txt）、自动关闭 Bandizip；执行/取消、实时进度条与日志、完成后统计报告（含打开目标文件夹）</td></tr>
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

<h3>压缩模式说明</h3>
<p>「开始」页面提供三种压缩模式：</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
<tr><th>一次压缩</th><th>二次压缩</th><th>行为</th></tr>
<tr><td>✔</td><td>✔</td><td>先创建 <code>1-4-First.zip</code>（分卷），再打包为 <code>1-4.zipp</code>（原来行为）</td></tr>
<tr><td>✔</td><td>✘</td><td>直接创建 <code>1-4.zip</code>（一次压缩，无 <code>-First</code> 后缀），不进行二次打包</td></tr>
<tr><td>✘</td><td>✘</td><td>跳过所有压缩，仅执行分类与重命名</td></tr>
</table>
<p>勾选「二次压缩」时自动同时勾选「一次压缩」；取消「一次压缩」时自动取消「二次压缩」。</p>

<hr>

<h3>注意事项</h3>
<ul>
  <li>本工具<b>仅支持 Bandizip</b>，必须安装并加入 PATH</li>
  <li>密码非空时 ≥ 8 位</li>
  <li>模板留空 = 不重命名，保留原始文件名</li>
  <li>扩展名映射默认不勾选，需手动选择</li>
  <li>勾选「保留原始文件」时源文件仅复制，压缩后副本自动清理</li>
  <li>取消勾选「一次压缩」跳过所有压缩，仅分类与重命名</li>
  <li>取消勾选「启用分卷」强制单文件输出，不进行分卷</li>
  <li>未启用压缩时，List.txt 只输出三列（序号/原文件名/新文件名），不含「所属压缩包名」</li>
  <li>首次使用建议先小范围测试</li>
</ul>
"""
