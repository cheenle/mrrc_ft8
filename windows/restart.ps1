# Restart rigctld + MRRC_FT8 server on Windows (mirrors restart.sh).
# Invoked detached by the server's /api/v1/devices/apply. Reads the same
# env as the launcher (%LOCALAPPDATA%\MRRC-FT8\ft8.env) and relaunches the
# bundled rigctld.exe and ft8-server.exe. Does not reopen the browser.
$ErrorActionPreference = "Stop"

$dataDir = Join-Path $env:LOCALAPPDATA "MRRC-FT8"
$appDir  = Split-Path $PSScriptRoot -Parent   # one dir above _internal

# ---- load env file ----
$envFile = Join-Path $dataDir "ft8.env"
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object {
        $_ -match "^\s*[A-Za-z0-9_]+\s*=" -and $_ -notmatch "^\s*#"
    } | ForEach-Object {
        $kv = $_ -split "=", 2
        [Environment]::SetEnvironmentVariable($kv[0].Trim(), $kv[1].Trim(), "Process")
    }
}

# ---- stop stale processes ----
Get-Process -Name "ft8-server","rigctld" -ErrorAction SilentlyContinue | Stop-Process -Force

# ---- rigctld ----
# Precedence matches restart.sh: device-config.json overrides env, which
# overrides the baked defaults below.
$deviceCfg = Join-Path $dataDir "data\device-config.json"
$rigModel = if ($env:MRRC_FT8_RIG_MODEL)  { $env:MRRC_FT8_RIG_MODEL }  else { "1049" }
$rigDevice = if ($env:MRRC_FT8_RIG_DEVICE) { $env:MRRC_FT8_RIG_DEVICE } else { "COM3" }
$rigBaud = if ($env:MRRC_FT8_RIG_BAUD)    { $env:MRRC_FT8_RIG_BAUD }   else { "38400" }
$rigStop = if ($env:MRRC_FT8_RIG_STOP_BITS) { $env:MRRC_FT8_RIG_STOP_BITS } else { "1" }
$rigPort = if ($env:MRRC_FT8_RIGCTLD_PORT)  { $env:MRRC_FT8_RIGCTLD_PORT }  else { "4532" }
if (Test-Path $deviceCfg) {
    $cfg = Get-Content $deviceCfg -Raw | ConvertFrom-Json
    if ($cfg.rig_model)    { $rigModel  = [string]$cfg.rig_model }
    if ($cfg.rig_device)   { $rigDevice = [string]$cfg.rig_device }
    if ($cfg.rig_baud)     { $rigBaud   = [string]$cfg.rig_baud }
    if ($cfg.rig_stop_bits){ $rigStop   = [string]$cfg.rig_stop_bits }
    if ($cfg.rigctld_port) { $rigPort   = [string]$cfg.rigctld_port }
}

$rigExe = Join-Path $appDir "hamlib\rigctld.exe"
if (Test-Path $rigExe) {
    $rigArgs = @("-m", $rigModel, "-r", $rigDevice, "-s", $rigBaud,
                 "-T", "127.0.0.1", "-t", $rigPort, "-vvv")
    if ($rigStop -ne "1") { $rigArgs += @("-C", "stop_bits=$rigStop") }
    Start-Process -FilePath $rigExe -ArgumentList $rigArgs -WindowStyle Hidden |
        Out-Null
}

# ---- server ----
$serverExe = Join-Path $appDir "ft8-server.exe"
if (Test-Path $serverExe) {
    Start-Process -FilePath $serverExe -WorkingDirectory $dataDir -WindowStyle Hidden |
        Out-Null
}
