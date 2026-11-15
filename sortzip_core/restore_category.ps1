# Restore matching archive volumes in this category only.
param([Parameter(Mandatory=$true)][string]$KitFolder,
      [Parameter(Mandatory=$true)][byte[]]$ManifestBytes,
      [switch]$Adjacent)

$ErrorActionPreference = 'Stop'
$zh = [Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq 'zh'
$root = if ($Adjacent) { $KitFolder } else { [IO.Path]::GetFullPath((Join-Path $KitFolder '..')) }
$lockName = if ($Adjacent) { '.sortzip-restore-names.lock' } else { 'Restore-Names.lock' }
$lockPath = Join-Path $KitFolder $lockName

function ItemAt($path) {
    return Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}
function CheckRegular($item) {
    if ($null -eq $item -or $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        if ($zh) { throw '需要普通文件。' } else { throw 'Expected a regular file.' }
    }
}
function FileHash($path) {
    $stream = [IO.File]::OpenRead($path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}
function SafeRelative($value) {
    if ($value -isnot [string] -or $value.Contains('\') -or $value.Length -eq 0) {
        if ($zh) { throw '还原路径无效。' } else { throw 'Invalid relative path.' }
    }
    $parts = $value.Split('/')
    foreach ($name in $parts) {
        if (!$name -or $name.EndsWith('.') -or $name.EndsWith(' ') -or
            $name.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or
            $name -match '^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)') {
            if ($zh) { throw "文件名无效：$name" } else { throw "Invalid name: $name" }
        }
    }
    return Join-Path $root ($parts -join '\')
}
function CheckParent($path) {
    $current = Split-Path -LiteralPath $path
    while ($current -and $current -ine $root) {
        $item = ItemAt $current
        if ($null -ne $item -and (!$item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))) {
            if ($zh) { throw "目录不安全：$current" } else { throw "Unsafe directory: $current" }
        }
        $current = Split-Path -LiteralPath $current
    }
}
function Matches($item,$row) {
    if ($null -eq $item -or $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -ne $row.size) {
        return $false
    }
    return (FileHash $item.FullName) -ieq $row.sha256
}
$lock = $null
try {
    foreach ($directory in @($root,$KitFolder)) {
        $item = ItemAt $directory
        if (!$item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            if ($zh) { throw "目录不安全：$directory" } else { throw "Unsafe directory: $directory" }
        }
    }
    $manifest = [Text.UTF8Encoding]::new($false,$true).GetString($ManifestBytes) | ConvertFrom-Json
    if ($manifest.schema -ne 2 -or @($manifest.files).Count -eq 0) {
        if ($zh) { throw '还原清单无效。' } else { throw 'Invalid manifest.' }
    }
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { $manifestHash = [BitConverter]::ToString($algorithm.ComputeHash($ManifestBytes)).Replace('-','').Substring(0,24) }
    finally { $algorithm.Dispose() }
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $queue = @()
    $index = 0
    foreach ($row in $manifest.files) {
        $from = SafeRelative $row.from
        $to = SafeRelative $row.to
        $overlaps = if ($Adjacent) {
            @($env:SORTZIP_RESTORE_CMD,$lockPath) -contains $from -or
            @($env:SORTZIP_RESTORE_CMD,$lockPath) -contains $to
        } else {
            $from.StartsWith($KitFolder+'\',[StringComparison]::OrdinalIgnoreCase) -or
            $to.StartsWith($KitFolder+'\',[StringComparison]::OrdinalIgnoreCase)
        }
        if ($overlaps) {
            if ($zh) { throw '还原目标与脚本文件冲突。' } else { throw 'Mapping overlaps the restore kit.' }
        }
        if ((Split-Path -LiteralPath $from) -ine (Split-Path -LiteralPath $to) -or
            $row.sha256 -notmatch '^[0-9a-fA-F]{64}$' -or
            ($row.size -isnot [int] -and $row.size -isnot [long]) -or $row.size -lt 0) {
            if ($zh) { throw '压缩包还原记录无效。' } else { throw 'Invalid archive mapping.' }
        }
        $temp = Join-Path (Split-Path -LiteralPath $from) ".sortzip-restore-$manifestHash-$index.tmp"
        $paths = @($from,$temp)
        if ($from -ine $to) { $paths += $to }
        foreach ($path in $paths) {
            if (!$names.Add($path)) {
                if ($zh) { throw "还原路径重复：$path" } else { throw "Duplicate restore path: $path" }
            }
        }
        $queue += [pscustomobject]@{from=$from; to=$to; temp=$temp; row=$row}
        $index++
    }
    $existing = ItemAt $lockPath
    if ($null -ne $existing) {
        CheckRegular $existing
        $lock = [IO.File]::Open($lockPath,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    } else {
        $lock = [IO.FileStream]::new($lockPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None,4096,[IO.FileOptions]::DeleteOnClose)
    }
    $restored = $already = $missing = $failed = $occupied = 0
    $checked = 0
    $total = $queue.Count
    $activity = if ($zh) { '还原压缩包后缀' } else { 'Restoring archive extensions' }
    $progressText = if ($zh) { '已检查 {0}/{1} 个卷' } else { 'Checked {0}/{1} volumes' }
    Write-Progress -Activity $activity -Status ($progressText -f $checked,$total) -PercentComplete 0
    $refresh = [Diagnostics.Stopwatch]::StartNew()
    foreach ($item in $queue) {
        try {
            CheckParent $item.from
            $from = ItemAt $item.from
            $to = ItemAt $item.to
            $temp = ItemAt $item.temp
            if ($item.from -ieq $item.to -and $null -ne $from) {
                if ($from.Name -ceq [IO.Path]::GetFileName($item.to)) { $from = $null }
                else { $to = $null }
            }
            if ($null -ne $to) {
                if ($null -eq $from -and $null -eq $temp -and (Matches $to $item.row)) { $already++; continue }
                $occupied++
                if ($zh) { Write-Host "目标已存在：$($item.to)" } else { Write-Host "Target already exists: $($item.to)" }
                continue
            }
            if ($null -ne $temp) {
                if ($null -ne $from -or !(Matches $temp $item.row)) {
                    $failed++
                    if ($zh) { Write-Host "无法继续还原暂存文件：$($item.temp)" } else { Write-Host "Cannot resume staged file: $($item.temp)" }
                    continue
                }
            } elseif ($null -eq $from) {
                $missing++
                if ($zh) { Write-Host "缺少文件：$($item.from)" } else { Write-Host "File missing: $($item.from)" }
                continue
            } else {
                if (!(Matches $from $item.row)) {
                    $failed++
                    if ($zh) { Write-Host "文件校验失败：$($item.from)" } else { Write-Host "File verification failed: $($item.from)" }
                    continue
                }
                [IO.File]::Move($item.from,$item.temp)
            }
            [IO.File]::Move($item.temp,$item.to)
            $restored++
        } catch {
            $failed++
            Write-Host "$($item.from): $($_.Exception.Message)"
        } finally {
            $checked++
            if ($checked -eq $total -or $refresh.ElapsedMilliseconds -ge 125) {
                Write-Progress -Activity $activity -Status ($progressText -f $checked,$total) `
                    -PercentComplete ([int](100 * $checked / $total))
                $refresh.Restart()
            }
        }
    }
    Write-Progress -Activity $activity -Completed
    if ($zh) {
        Write-Host "已还原：$restored。此前已还原：$already。缺失：$missing。失败：$failed。名称占用：$occupied。"
    } else {
        Write-Host "Restored: $restored. Already restored: $already. Missing: $missing. Failed: $failed. Occupied: $occupied."
    }
    if ($missing+$failed+$occupied -gt 0) { exit 2 }
} catch {
    Write-Host $_.Exception.Message
    exit 1
} finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
