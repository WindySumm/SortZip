[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Get-Item -LiteralPath (Split-Path -Parent $PSScriptRoot)).FullName
$results = [System.Collections.Generic.List[object]]::new()
$tracked = @()
if (Test-Path -LiteralPath (Join-Path $projectRoot '.git')) {
    $tracked = @(git -C $projectRoot -c core.quotePath=false ls-files -- tests sortzip_core scripts .build-validation)
    if ($LASTEXITCODE -ne 0) { throw 'Could not check which files Git tracks.' }
}

function Test-Link($Item) {
    return [bool]($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Assert-LocalFolder([string]$Path) {
    if (!(Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Cleanup folder is not a directory: $Path"
    }
    for ($folder = Get-Item -LiteralPath $path -Force; $folder -and $folder.FullName -ne $projectRoot; $folder = $folder.Parent) {
        if ($folder -isnot [IO.DirectoryInfo] -or (Test-Link $folder)) {
            throw "Cleanup folder is a link or is not a directory: $($folder.FullName)"
        }
    }
    if (!$folder) { throw "Cleanup folder lies outside the repository: $path" }
}

function Add-Candidates([string]$Relative, [string]$Files, [string]$Directories) {
    $path = Join-Path $projectRoot $Relative
    if (!(Test-Path -LiteralPath $path)) { return }
    Assert-LocalFolder $path
    foreach ($item in Get-ChildItem -LiteralPath $path -Force) {
        if ($item.PSIsContainer) {
            if ($Directories -and $item.Name -match $Directories) { $results.Add($item) }
        } elseif ($Files -and $item.Name -match $Files) {
            $results.Add($item)
        }
    }
}

function Get-ArtifactBytes($Item) {
    if (Test-Link $Item) { throw "Link inside cleanup target: $($Item.FullName)" }
    if (!$Item.PSIsContainer) { return $Item.Length }
    [long]$length = 0
    foreach ($child in Get-ChildItem -LiteralPath $Item.FullName -Force) {
        $length += Get-ArtifactBytes $child
    }
    return $length
}

$bytecode = '^[\w.\-]+\.py[co]$'
Add-Candidates 'tests/__pycache__' $bytecode '^tmp[a-z0-9_]{8}$'
foreach ($folder in 'sortzip_core/__pycache__','scripts/__pycache__','.build-validation/__pycache__') {
    Add-Candidates $folder $bytecode ''
}
Add-Candidates '.build-validation' (
    '^(?:build-v[\d.]+-smoke\.json|(?:cleanup|english|final-light-blue|layout|retention|v1-final|v[\d.]+)-[\w.\-]+\.(?:log|json|png)|preview-(?:optimized|many-archives)\.json)$'
) '^(?:(?:package|retention-pack)-[a-f0-9]{32}|sortzip-(?:smoke|verify)-[a-z0-9_]{8})$'
Add-Candidates '.build-validation/acceptance' '' '^gui-[a-f0-9]{8}$'
Add-Candidates '.build-validation/benchmarks' '' '^[a-f0-9]{8}$'

$removed = 0
$failed = 0
[long]$bytes = 0
[long]$matchedBytes = 0
$prefix = $projectRoot + [IO.Path]::DirectorySeparatorChar
foreach ($item in $results) {
    try {
        $current = Get-Item -LiteralPath $item.FullName -Force
        if (!$current.FullName.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Cleanup target lies outside the repository: $($current.FullName)"
        }
        Assert-LocalFolder (Split-Path -Parent $current.FullName)
        $relative = $current.FullName.Substring($projectRoot.Length + 1).Replace('\','/')
        if ($tracked.Where({ $_ -eq $relative -or $_.StartsWith("$relative/", [StringComparison]::OrdinalIgnoreCase) }, 'First').Count) {
            throw "Git-tracked files in cleanup target: $relative"
        }
        $size = Get-ArtifactBytes $current
        $matchedBytes += $size
        if ($PSCmdlet.ShouldProcess($relative, 'Remove generated test artifacts')) {
            Remove-Item -LiteralPath $current.FullName -Recurse:$current.PSIsContainer -Force
            $removed++
            $bytes += $size
        }
    } catch {
        Write-Warning $_.Exception.Message
        $failed++
    }
}
Write-Host ("Matched {0} item(s), {1:N0} bytes. Removed {2} item(s), {3:N0} bytes. Skipped due to errors: {4}." -f $results.Count, $matchedBytes, $removed, $bytes, $failed)
if ($failed) { throw 'Some generated artifacts could not be removed.' }
