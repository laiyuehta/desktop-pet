# Create backend\.venv for the R7 backend (#38: no longer uses the ComfyUI embedded python).
# Usage (repo root or anywhere):  powershell -ExecutionPolicy Bypass -File backend\setup_venv.ps1 [-Python "py -3"]
# Installs backend\requirements.txt (first line: -e ..\shared). The backend itself only needs the standard library.
param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$backend = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $backend '.venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    if ($Python) { & cmd /c "$Python -m venv `"$venv`"" }
    elseif (Get-Command py.exe -ErrorAction SilentlyContinue) { & py.exe -3 -m venv $venv }
    else { & python.exe -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed: $venv" }
}
$py = Join-Path $venv 'Scripts\python.exe'
Push-Location $backend
try {
    & $py -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
} finally { Pop-Location }
& $py -c "import shared, sys; print('ok', sys.executable)"
