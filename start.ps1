# One-command start without Docker (Windows PowerShell 5.1 or 7):
#   Mosquitto -> Twin (--fake by default) -> Guardian stand-in -> Brain stand-in -> timeline.
#
#   .\start.ps1                 start everything in the background, logs in logs\services\
#   .\start.ps1 -Sumo           run the Twin on SUMO instead of --fake
#   .\start.ps1 -Windows        one visible console window per service instead of background processes
#   .\start.ps1 -NoMocks        do not start our stand-ins (the partner teams run their real Brain and Guardian;
#                               point GUARDIAN_URL / BRAIN_URL at them in .env)
#   .\scripts\stop_all.ps1      stop everything again
#
# Settings: twin\city.yaml, twin\mocks\mocks.yaml, and .env (copied from .env.example) which overrides both.
# If PowerShell refuses to run scripts:  powershell -ExecutionPolicy Bypass -File .\start.ps1
param([switch]$Sumo, [switch]$Windows, [switch]$NoMocks, [double]$Speed = 1.0)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$logDir = Join-Path $root "logs\services"
New-Item -ItemType Directory -Force $logDir | Out-Null

# --- .env: KEY=VALUE lines become environment variables for every service started below ---
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $l = ($line -split '#', 2)[0].Trim()
        if ($l -match '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$' -and $Matches[2] -ne '') {
            Set-Item -Path ("Env:" + $Matches[1]) -Value $Matches[2].Trim()
        }
    }
    Write-Host "loaded .env"
}
$env:PYTHONUNBUFFERED = "1"

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

function Test-Port([int]$port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $port); return $true } catch { return $false } finally { $c.Close() }
}

function Wait-Url([string]$name, [string]$url, [int]$seconds) {
    $end = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $end) {
        try { Invoke-RestMethod -Uri $url -TimeoutSec 2 | Out-Null; Write-Host ("  {0,-9} up    {1}" -f $name, $url); return $true }
        catch { Start-Sleep -Milliseconds 500 }
    }
    Write-Warning ("{0} did not answer {1} within {2} s; see logs\services\{3}.log" -f $name, $url, $seconds, $name.ToLower())
    return $false
}

function Start-Layer([string]$name, [string[]]$pyArgs) {
    if ($Windows) {
        $cmd = "`$host.UI.RawUI.WindowTitle = '$name'; & '$python' " + (($pyArgs | ForEach-Object { "'$_'" }) -join ' ')
        Start-Process powershell -ArgumentList @("-NoExit", "-Command", $cmd) -WorkingDirectory $root | Out-Null
    } else {
        $out = Join-Path $logDir "$($name.ToLower()).log"
        $err = Join-Path $logDir "$($name.ToLower()).err.log"
        Start-Process -FilePath $python -ArgumentList $pyArgs -WorkingDirectory $root -WindowStyle Hidden `
            -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null
    }
}

$mqttPort = if ($env:MQTT_PORT) { [int]$env:MQTT_PORT } else { 1883 }
$mqttHost = if ($env:MQTT_HOST) { $env:MQTT_HOST } else { "127.0.0.1" }

# --- 1. Mosquitto ---
if ($mqttHost -in @("127.0.0.1", "localhost") -and -not (Test-Port $mqttPort)) {
    $svc = Get-Service mosquitto -ErrorAction SilentlyContinue
    if ($svc) {
        try { Start-Service mosquitto; Write-Host "started the Mosquitto service" }
        catch { Write-Warning "could not start the Mosquitto service (needs an administrator shell): $_" }
    }
    if (-not (Test-Port $mqttPort)) {
        $exe = (Get-Command mosquitto -ErrorAction SilentlyContinue).Source
        if (-not $exe -and (Test-Path "C:\Program Files\mosquitto\mosquitto.exe")) { $exe = "C:\Program Files\mosquitto\mosquitto.exe" }
        if (-not $exe) { throw "Mosquitto is not running and mosquitto.exe was not found. Install it (README section 6) or use docker compose up." }
        Start-Process -FilePath $exe -ArgumentList @("-p", "$mqttPort") -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logDir "mosquitto.log") -RedirectStandardError (Join-Path $logDir "mosquitto.err.log") | Out-Null
        Start-Sleep -Seconds 1
    }
}
if ($mqttHost -in @("127.0.0.1", "localhost") -and -not (Test-Port $mqttPort)) { throw "no MQTT broker on port $mqttPort" }
Write-Host ("  {0,-9} up    mqtt://{1}:{2}" -f "Mosquitto", $mqttHost, $mqttPort)

# --- 2. Twin first: the stand-ins read its device list ---
if (Test-Port 8000) { Write-Warning "something already listens on port 8000; run .\scripts\stop_all.ps1 first if it is an old Twin" }
$twinArgs = @("twin\src\twin.py", "--speed", "$Speed")
if (-not $Sumo) { $twinArgs += "--fake" }
Start-Layer "Twin" $twinArgs
$twinUp = Wait-Url "Twin" "http://127.0.0.1:8000/health" 60

# --- 3. our stand-ins (rule R1), unless the partner teams' layers are used ---
if (-not $NoMocks) {
    Start-Layer "Guardian" @("twin\mocks\guardian_mock.py")
    Wait-Url "Guardian" "http://127.0.0.1:8001/health" 30 | Out-Null
    Start-Layer "Brain" @("twin\mocks\brain_mock.py")
    Wait-Url "Brain" "http://127.0.0.1:8003/health" 30 | Out-Null
}

# --- 4. timeline (rule R3) ---
Start-Layer "Timeline" @("timeline\timeline.py")
Wait-Url "Timeline" "http://127.0.0.1:8002/history" 30 | Out-Null

Write-Host ""
Write-Host "Timeline:   http://127.0.0.1:8002"
Write-Host "Twin API:   http://127.0.0.1:8000/docs   (contract: /contract)"
Write-Host "Scenario:   $python scenarios\run_scenario.py t3        (or: all, --list)"
Write-Host "Stop:       .\scripts\stop_all.ps1"
if (-not $Windows) { Write-Host "Logs:       logs\services\" }
if (-not $twinUp) { exit 1 }
