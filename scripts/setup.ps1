param([switch]$InstallAI)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error 'Python was not found. Install 64-bit Python 3.11 or 3.12 with Tkinter.'
}
$versionText = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($versionText -notin @('3.11', '3.12')) {
    Write-Error "Python $versionText is not supported. Use Python 3.11 or 3.12."
}
& $python.Source -m venv "$projectRoot\.venv"
$venvPython = "$projectRoot\.venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r "$projectRoot\requirements.txt"
if ($InstallAI) {
    & $venvPython -m pip install -r "$projectRoot\requirements-ai.txt"
}
& $venvPython "$PSScriptRoot\verify_installation.py"
& $venvPython -m unittest discover -s "$projectRoot\tests" -v
Write-Host 'Setup and verification completed. Run run.bat to start the application.' -ForegroundColor Green
