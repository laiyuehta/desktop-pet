# Create backend\.venv for the R7 backend (#38: no longer uses the ComfyUI embedded python).
# Usage:  powershell -ExecutionPolicy Bypass -File backend\setup_venv.ps1 [-Python C:\path\to\python.exe]
# Installs backend\requirements.txt (first line: -e ..\shared). The backend itself only needs the standard library.
param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$backend = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $backend '.venv'
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
    if ($Python) { & $Python -m venv $venv }
    elseif (Get-Command py.exe -ErrorAction SilentlyContinue) { & py.exe -3 -m venv $venv }
    else { & python.exe -m venv $venv }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) { throw "venv creation failed: $venv" }
}
Push-Location $backend
try {
    & $py -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
} finally { Pop-Location }
# Check from a neutral directory so the repo's shared\ folder is not picked up by accident
Push-Location $env:TEMP
try {
    & $py -c "import shared.apikey, sys; print('ok', sys.executable)"
    if ($LASTEXITCODE -ne 0) { throw "shared is not importable from $venv" }
} finally { Pop-Location }
