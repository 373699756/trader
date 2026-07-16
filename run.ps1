Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSCommandPath
$Mode = if ($args.Count -gt 0) { $args[0] } else { "serve" }

function Show-Usage {
    @"
用法: .\run.ps1 [serve|validate-config]

环境变量:
  TRADER_CONFIG=C:\absolute\path\runtime.json
  TRADER_HOST=127.0.0.1
  TRADER_PORT=5000
  DEEPSEEK_API_KEY=...
  FORCE_INSTALL_DEPS=1
"@ | Write-Host
}

if ($Mode -eq "-h" -or $Mode -eq "--help") {
    Show-Usage
    exit 0
}

$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { Join-Path $RootDir ".venv" }
$IsWindowsHost = -not $PSVersionTable.ContainsKey("Platform") -or $PSVersionTable.Platform -eq "Win32NT"
$VenvPython = if ($IsWindowsHost) { Join-Path $VenvDir "Scripts\python.exe" } else { Join-Path $VenvDir "bin/python" }
$Server = if ($IsWindowsHost) { Join-Path $VenvDir "Scripts\trader-server.exe" } else { Join-Path $VenvDir "bin/trader-server" }
$Cli = if ($IsWindowsHost) { Join-Path $VenvDir "Scripts\trader-cli.exe" } else { Join-Path $VenvDir "bin/trader-cli" }
$ConfigPath = if ($env:TRADER_CONFIG) { $env:TRADER_CONFIG } else { Join-Path $RootDir "config\v2\runtime.json" }

if (-not (Test-Path $VenvPython)) {
    $Launcher = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $Launcher) {
        $Launcher = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if ($null -eq $Launcher) {
        $Launcher = Get-Command py -ErrorAction SilentlyContinue
    }
    if ($null -eq $Launcher) {
        throw "需要 Python 3.10-3.14。"
    }
    $LauncherPrefix = if ($Launcher.Name -eq "py.exe" -or $Launcher.Name -eq "py") { @("-3") } else { @() }
    & $Launcher.Source @LauncherPrefix -c "import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] < (3, 15)))"
    if ($LASTEXITCODE -ne 0) {
        throw "需要 Python 3.10-3.14。"
    }
    & $Launcher.Source @LauncherPrefix -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$NeedsInstall = -not (Test-Path $Server)
if (-not $NeedsInstall) {
    $NeedsInstall = (Get-Item (Join-Path $RootDir "pyproject.toml")).LastWriteTimeUtc -gt (Get-Item $Server).LastWriteTimeUtc
}
if ($NeedsInstall -or $env:FORCE_INSTALL_DEPS -eq "1") {
    & $VenvPython -m pip install --disable-pip-version-check -e $RootDir
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

if (-not $env:TRADER_HOST) {
    $env:TRADER_HOST = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
}
if (-not $env:TRADER_PORT) {
    $env:TRADER_PORT = if ($env:PORT) { $env:PORT } else { "5000" }
}

if ($Mode -eq "serve" -or $Mode -eq "app") {
    & $Server --config $ConfigPath
    exit $LASTEXITCODE
}
if ($Mode -eq "validate-config") {
    & $Cli --config $ConfigPath validate-config
    exit $LASTEXITCODE
}

Write-Error "未知模式: $Mode"
Show-Usage
exit 2
