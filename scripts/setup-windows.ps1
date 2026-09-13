# One-click prep for XFER Explorer on Windows.
# No secrets: never writes rpcuser/rpcpassword, cookies, seeds, or keys.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$WalletVersion = "1.0.13"
$WalletUrl = "https://github.com/NiftyRaven/x-coin/releases/download/v$WalletVersion/X-Coin-1.0.13-Windows.zip"
$WalletHome = Join-Path $env:LOCALAPPDATA "XCoin-Wallet\$WalletVersion"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg"
}

function Get-PythonExe {
    foreach ($cmd in @("python", "py")) {
        $p = Get-Command $cmd -ErrorAction SilentlyContinue
        if (-not $p) { continue }
        try {
            if ($cmd -eq "py") {
                $ver = & py -3 -c "import sys; print(sys.version_info.major, sys.version_info.minor)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $ver) { return "py -3" }
            } else {
                $ver = & python -c "import sys; print('%d.%d' % (sys.version_info.major, sys.version_info.minor))" 2>$null
                if ($ver -match '^3\.(1[1-9]|[2-9]\d)') { return "python" }
                if ($ver -match '^3\.') { return "python" }
            }
        } catch { }
    }
    return $null
}

function Install-Python {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Installing Python 3.12 with winget (one-time)..."
        & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        return
    }
    Write-Host "Python 3.11+ is required. Opening the official installer page..."
    Start-Process "https://www.python.org/downloads/windows/"
    throw "Install Python, tick 'Add python.exe to PATH', then run SETUP.bat again."
}

function Ensure-ConfLine([string]$path, [string]$key, [string]$value) {
    $dir = Split-Path -Parent $path
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }
    $pattern = "^\s*$([regex]::Escape($key))\s*="
    if (Test-Path $path) {
        $lines = Get-Content -LiteralPath $path
        foreach ($line in $lines) {
            if ($line -match $pattern -and $line -notmatch "^\s*#") { return }
        }
        Add-Content -LiteralPath $path -Value "`r`n$key=$value"
        Write-Host "Added $key=$value to $path"
        return
    }
    @(
        "# Created by XFER Explorer SETUP.bat — no passwords.",
        "server=1",
        "rpcbind=127.0.0.1",
        "rpcallowip=127.0.0.1"
    ) | Set-Content -LiteralPath $path -Encoding ascii
    Write-Host "Created $path (template, no secrets)"
}

function Find-WalletExe {
    if ($env:XCOIN_WALLET -and (Test-Path -LiteralPath $env:XCOIN_WALLET)) {
        return $env:XCOIN_WALLET
    }
    $names = @("X Coin Wallet.exe", "xcoin-qt.exe")
    $roots = @(
        $Root,
        (Split-Path -Parent $Root),
        $WalletHome,
        (Join-Path $env:USERPROFILE "Downloads\X-Coin-1.0.13-Windows"),
        (Join-Path $env:USERPROFILE "Downloads")
    ) | Where-Object { $_ -and (Test-Path $_) }
    foreach ($base in $roots) {
        foreach ($name in $names) {
            $direct = Join-Path $base $name
            if (Test-Path -LiteralPath $direct) { return $direct }
        }
        $hit = Get-ChildItem -LiteralPath $base -Filter "X Coin Wallet.exe" -Depth 4 -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch "Practice" } |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Install-Wallet {
    Write-Host "Downloading official X Coin $WalletVersion (no keys in this repo)..."
    $zip = Join-Path $env:TEMP "X-Coin-$WalletVersion-Windows.zip"
    New-Item -ItemType Directory -Force -Path $WalletHome | Out-Null
    Invoke-WebRequest -Uri $WalletUrl -OutFile $zip -UseBasicParsing
    Expand-Archive -LiteralPath $zip -DestinationPath $WalletHome -Force
    Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
    $exe = Get-ChildItem -LiteralPath $WalletHome -Filter "X Coin Wallet.exe" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch "Practice" } |
        Select-Object -First 1
    if (-not $exe) { throw "Downloaded the wallet zip but could not find X Coin Wallet.exe." }
    return $exe.FullName
}

function Wallet-Running {
    return [bool](Get-Process -Name "xcoin-qt", "xcoind", "X Coin Wallet" -ErrorAction SilentlyContinue)
}

function Wait-Rpc {
    $cookie = Join-Path $env:APPDATA "XCoin\.cookie"
    Write-Host "Waiting for wallet RPC (port 38442 or .cookie)..."
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-Path -LiteralPath $cookie) { return $true }
        try {
            $listen = Get-NetTCPConnection -LocalPort 38442 -State Listen -ErrorAction SilentlyContinue
            if ($listen) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

Write-Host "XFER Explorer one-click setup (Windows)"
Write-Host "This does not spend coins and does not write RPC passwords."

Write-Step "Python"
$py = Get-PythonExe
if (-not $py) {
    Install-Python
    $py = Get-PythonExe
}
if (-not $py) { throw "Python is still not on PATH. Close this window, open a new one, run SETUP.bat again." }
Write-Host "Using $py"

Write-Step "X Coin wallet 1.0.13+"
$wallet = Find-WalletExe
if (-not $wallet) {
    $wallet = Install-Wallet
}
Write-Host "Wallet: $wallet"

Write-Step "Enable local RPC (server=1 only — no passwords)"
$packaged = Join-Path (Split-Path -Parent $wallet) "xcoin.conf"
$dataConf = Join-Path $env:APPDATA "XCoin\xcoin.conf"
Ensure-ConfLine $packaged "server" "1"
Ensure-ConfLine $packaged "rpcbind" "127.0.0.1"
Ensure-ConfLine $packaged "rpcallowip" "127.0.0.1"
Ensure-ConfLine $dataConf "server" "1"
Ensure-ConfLine $dataConf "rpcbind" "127.0.0.1"
Ensure-ConfLine $dataConf "rpcallowip" "127.0.0.1"

Write-Step "Start wallet if needed"
if (Wallet-Running) {
    Write-Host "Wallet already running. Leave it open."
} else {
    Start-Process -FilePath $wallet
    Write-Host "Started X Coin Wallet. Finish any first-run 12-word screen in that window."
}
if (-not (Wait-Rpc)) {
    Write-Host "RPC is not up yet. When the wallet has finished loading, run start.bat."
    Write-Host "If it stays offline, see docs\TROUBLESHOOTING.md"
    exit 2
}

Write-Host ""
Write-Host "Wallet RPC is ready. Starting the explorer..."
exit 0
