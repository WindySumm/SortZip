param([string]$Python = '.\.venv\Scripts\python.exe')

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    # Keep validation output separate from the user's existing dist/SortZip.exe.
    $validationRoot = Join-Path $projectRoot '.build-validation'
    New-Item -ItemType Directory -Force -Path $validationRoot | Out-Null
    $previousCache = $env:PYINSTALLER_CONFIG_DIR
    $previousPath = $env:PATH
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $validationRoot 'cache'
    try {
        # Prevent unrelated DLL directories in the host PATH from entering the bundle.
        $basePython = & $Python -c 'import sys; print(sys.base_prefix)'
        if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect Python environment' }
        $env:PATH = @((Join-Path $projectRoot '.venv\Scripts'), $basePython,
                      (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
        & $Python -m PyInstaller --noconfirm --clean `
            --distpath (Join-Path $validationRoot 'dist') `
            --workpath (Join-Path $validationRoot 'build') `
            (Join-Path $projectRoot 'SortZip.spec')
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
    } finally {
        $env:PYINSTALLER_CONFIG_DIR = $previousCache
        $env:PATH = $previousPath
    }
    $executable = Join-Path $validationRoot 'dist\SortZip.exe'
    $checksum = "$executable.sha256"
    $digest = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $checksum -Value "$digest  SortZip.exe" -Encoding Ascii
    Write-Output "Validation executable: $executable"
    Write-Output "SHA-256: $checksum"
} finally {
    Pop-Location
}
