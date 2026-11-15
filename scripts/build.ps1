param(
    [string]$Python = '.\.venv\Scripts\python.exe',
    [switch]$Validation
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
Push-Location -LiteralPath $projectRoot
$scratch = $null
$previousCache = $env:PYINSTALLER_CONFIG_DIR
$previousPath = $env:PATH
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
try {
    $validationRoot = Join-Path $projectRoot '.build-validation'
    $releaseRoot = Join-Path $projectRoot 'release'
    $outputRoots = if ($Validation) { @($validationRoot) } else { @($validationRoot, $releaseRoot) }
    foreach ($directory in $outputRoots) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
        if ((Get-Item -LiteralPath $directory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Output directory is a link: $directory"
        }
    }
    $pythonPath = (Get-Command $Python -CommandType Application -ErrorAction Stop).Source
    $version = & $pythonPath -c 'from sortzip_core import __version__; print(__version__)'
    if ($LASTEXITCODE -ne 0 -or $version -cnotmatch '^v\d+\.\d+\.\d+$') { throw 'Invalid SortZip version' }
    $basePython = & $pythonPath -c 'import sys; print(sys.base_prefix)'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect Python environment' }

    $scratch = Join-Path $validationRoot ('package-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $scratch | Out-Null
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $scratch 'cache'
    $env:TEMP = $scratch
    $env:TMP = $scratch
    # Keep unrelated DLL directories out of the bundle.
    $env:PATH = @((Split-Path -Parent $pythonPath), $basePython,
                  (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & $pythonPath -m PyInstaller --noconfirm --clean --distpath $scratch `
        --workpath (Join-Path $scratch 'build') (Join-Path $projectRoot 'SortZip.spec')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
    $env:PATH = $previousPath

    $candidate = Join-Path $scratch 'SortZip.exe'
    $report = Join-Path $scratch 'smoke.json'
    $result = $null
    $process = Start-Process -FilePath $candidate -ArgumentList @('--smoke-test', ('"' + $report + '"')) `
        -WindowStyle Hidden -Wait -PassThru
    if (Test-Path -LiteralPath $report) {
        if (!$Validation) {
            Copy-Item -LiteralPath $report -Destination (Join-Path $validationRoot "build-$version-smoke.json")
        }
        $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    }
    if ($process.ExitCode -ne 0 -or !$result -or $result.status -ne 'ok' -or $result.version -ne $version) {
        throw "Packaged startup check failed: $($result.error)"
    }

    if ($Validation) {
        $executable = Join-Path $validationRoot 'SortZip-validation.exe'
        $existing = Get-Item -LiteralPath $executable -Force -ErrorAction SilentlyContinue
        if ($existing) {
            if ($existing.PSIsContainer -or ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Unexpected validation executable path: $executable"
            }
            [IO.File]::Replace($candidate, $executable, [NullString]::Value)
        } else {
            [IO.File]::Move($candidate, $executable)
        }
        Copy-Item -LiteralPath $report -Destination (Join-Path $validationRoot 'latest-validation-smoke.json') -Force
        Write-Output "Validation executable ($version): $executable"
        return
    }

    $filename = "SortZip-$version.exe"
    $executable = Join-Path $releaseRoot $filename
    $checksum = "$executable.sha256"
    foreach ($file in @($executable, $checksum)) {
        if (Test-Path -LiteralPath $file) {
            $existing = Get-Item -LiteralPath $file -Force
            if ($existing.PSIsContainer -or ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Unexpected release path: $file"
            }
        }
    }
    $digest = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
    Move-Item -LiteralPath $candidate -Destination $executable -Force
    Set-Content -LiteralPath $checksum -Value "$digest  $filename" -Encoding Ascii
    Write-Output "Release executable: $executable"
    Write-Output "SHA-256: $checksum"
} finally {
    $env:PYINSTALLER_CONFIG_DIR = $previousCache
    $env:PATH = $previousPath
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    Pop-Location
    if ($scratch -and (Test-Path -LiteralPath $scratch)) {
        $resolved = (Resolve-Path -LiteralPath $scratch).Path
        if ($resolved -ne $scratch -or (Split-Path -Parent $resolved) -ne $validationRoot -or
            ((Get-Item -LiteralPath $resolved -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            (Get-ChildItem -LiteralPath $resolved -Force -Recurse | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })) {
            throw "Unexpected build scratch path: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Force -Recurse
    }
}
