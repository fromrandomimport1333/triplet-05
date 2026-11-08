# .\scenarios\run.ps1 B3   (same arguments as scenarios/run.py)
$py = Join-Path $PSScriptRoot "..\brain\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py (Join-Path $PSScriptRoot "run.py") @args
exit $LASTEXITCODE
