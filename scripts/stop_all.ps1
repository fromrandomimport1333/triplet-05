# Stops every Python process started from this project (Twin, Guardian, Brain, timeline, stand-ins).
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -and ($_.CommandLine -like "*$here*" -or $_.CommandLine -match '(twin|guardian|brain|timeline|guardian_mock|brain_mock)\.py') } |
  ForEach-Object { Write-Host "stopping $($_.ProcessId): $($_.CommandLine)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
