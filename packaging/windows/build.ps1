$ErrorActionPreference = "Stop"

# $ErrorActionPreference does NOT apply to native commands - check
# $LASTEXITCODE explicitly so a failing test or build aborts packaging.
function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)]$Remaining
    )
    $flat = @()
    foreach ($a in $Remaining) { $flat += $a }
    & $Command @flat
    if ($LASTEXITCODE -ne 0) {
        throw "$Command $($flat -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$DistRoot = Join-Path $RepoRoot "dist\windows"
$AppRoot = Join-Path $DistRoot "MRRC_FT8"
$PyInstallerRoot = Join-Path $DistRoot "_pyinstaller"
$DllDir = Join-Path $RepoRoot "vendor\wsjtx-runtime\windows\x64"

Set-Location $RepoRoot

# 1. tests (must be green before packaging)
Invoke-Checked python -m pytest tests/ -q

# 2. desktop client build (dist/ is gitignored)
Push-Location (Join-Path $RepoRoot "desktop\ft8web")
Invoke-Checked npm ci
Invoke-Checked npm run build
Pop-Location

# 3. sanity-check vendored binaries (mirrors mrrc_ft710's FTDI warning)
if (!(Test-Path (Join-Path $DllDir "wsjt_core.dll"))) {
    Write-Warning "wsjt_core.dll missing. Build it on the VM once (see win_pack.md) and copy it to:"
    Write-Warning "  $DllDir"
}
$rigExe = Join-Path $RepoRoot "vendor\hamlib\windows\x64\rigctld.exe"
if (!(Test-Path $rigExe)) {
    Write-Warning "rigctld.exe missing. Download the Hamlib Windows build to:"
    Write-Warning "  $rigExe"
}

# 4. PyInstaller
Invoke-Checked pyinstaller packaging\windows\ft8_server.spec --noconfirm --distpath "$PyInstallerRoot" --workpath "build\pyinstaller"
Invoke-Checked pyinstaller packaging\windows\ft8_launcher.spec --noconfirm --distpath "$PyInstallerRoot" --workpath "build\pyinstaller"

# 5. assemble the app directory
if (Test-Path $AppRoot) { Remove-Item $AppRoot -Recurse -Force }
New-Item -ItemType Directory -Path $AppRoot | Out-Null
Copy-Item (Join-Path $PyInstallerRoot "ft8-server\*") $AppRoot -Recurse -Force
Copy-Item (Join-Path $PyInstallerRoot "MRRC_FT8.exe") $AppRoot -Force
Copy-Item (Join-Path $RepoRoot "windows") $AppRoot -Recurse -Force
Remove-Item (Join-Path $AppRoot "windows\__pycache__") -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path (Join-Path $RepoRoot "vendor\hamlib\windows\x64")) {
    $dest = Join-Path $AppRoot "hamlib"
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    Copy-Item (Join-Path $RepoRoot "vendor\hamlib\windows\x64\*") $dest -Recurse -Force
}

# server spec datas put hamlib in _internal; {app}\hamlib is authoritative
Remove-Item (Join-Path $AppRoot "_internal\hamlib") -Recurse -Force -ErrorAction SilentlyContinue

# 6. Inno Setup
if (Get-Command iscc -ErrorAction SilentlyContinue) {
    Invoke-Checked iscc packaging\windows\MRRC-FT8.iss
} else {
    Write-Warning "Inno Setup Compiler 'iscc' not found. Install Inno Setup and rerun to create the Setup EXE."
}

Write-Host "Installer output: $(Join-Path $DistRoot 'MRRC_FT8-Setup.exe')"
