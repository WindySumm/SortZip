MANUAL_TEXT = """<h2>命名模板使用说明</h2>

<p>在「命名」页面中，你可以为每个分类文件夹单独设置命名规则。</p>

<hr>

<h3>基本规则</h3>
<ul>
  <li><b>模板留空</b> ＝ 不进行重命名，保留原始文件名</li>
  <li><b>模板非空</b> ＝ 按照模板中的占位符生成新文件名</li>
  <li>每条规则可以单独启用/禁用</li>
  <li>匹配文件夹为 <code>*</code> 时，匹配所有未被其他规则匹配的文件夹</li>
</ul>

<hr>

<h3>可用占位符</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
<tr><th>占位符</th><th>说明</th><th>示例值</th></tr>
<tr><td><code>{n}</code></td><td>序号（从 1 开始递增）</td><td><code>1</code>、<code>2</code>、<code>3</code></td></tr>
<tr><td><code>{ext}</code></td><td>文件扩展名（含小数点）</td><td><code>.jpg</code>、<code>.docx</code></td></tr>
<tr><td><code>{folder}</code></td><td>文件所在的文件夹名</td><td><code>照片</code>、<code>文档</code></td></tr>
<tr><td><code>{original}</code></td><td>原始文件名（不含扩展名）</td><td><code>IMG_001</code>、<code>photo_2024</code></td></tr>
</table>

<hr>

<h3>预设模板示例</h3>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
<tr><th>模板</th><th>原始文件名</th><th>重命名后</th></tr>
<tr><td><code>{n}{ext}</code></td><td>IMG_001.jpg</td><td>1.jpg</td></tr>
<tr><td><code>{folder}_{n}{ext}</code></td><td>IMG_001.jpg</td><td>照片_1.jpg</td></tr>
<tr><td><code>{original}{ext}</code></td><td>IMG_001.jpg</td><td>IMG_001.jpg</td></tr>
<tr><td><code>{original}_{n}{ext}</code></td><td>IMG_001.jpg</td><td>IMG_001_1.jpg</td></tr>
<tr><td><code>{folder}_{original}{ext}</code></td><td>IMG_001.jpg</td><td>照片_IMG_001.jpg</td></tr>
</table>

<hr>

<h3>常见用法</h3>
<ul>
  <li><b>整理照片</b>：模板 <code>{n}{ext}</code> → 照片文件夹内文件变为 1.jpg、2.jpg…</li>
  <li><b>区分来源</b>：模板 <code>{folder}_{n}{ext}</code> → 照片_1.jpg、文档_1.docx…</li>
  <li><b>保留原名</b>：模板留空即可</li>
  <li><b>带前缀编号</b>：模板 <code>{original}_{n}{ext}</code> → IMG_001_1.jpg</li>
</ul>
"""

__all__ = ["MANUAL_TEXT"]
