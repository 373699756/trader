Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSCommandPath
$Mode = if ($args.Count -gt 0) { $args[0] } else { "serve" }

function Show-Usage {
    @"
本地 A 股研究看板

日常使用（不做离线研究）:
  .\run.ps1                         启动本地 A 股研究看板（推荐）
  .\run.ps1 serve                   显式启动，等同于无参数运行
  .\run.ps1 validate-config         校验配置后退出，不启动服务
  .\run.ps1 research-status         只读查看研究数据准备状态
  .\run.ps1 performance-check       离线运行活动生产函数性能门禁
  .\run.ps1 help                    查看本帮助

离线研究（仅在明确执行研究任务时使用）:
  .\run.ps1 research-history-download        下载并续传离线历史日线归档
  .\run.ps1 research-backtest                只读运行固定历史回测
  .\run.ps1 research-r6-screen                运行并封存 R6 历史筛选
  .\run.ps1 research-r6-daily-screen          运行并封存 R6 日线趋势筛选
  .\run.ps1 research-r6-stability-screen      运行并封存 R6 稳定性诊断
  .\run.ps1 research-tomorrow-p2-screen       运行并封存 Tomorrow P2 历史筛选
  .\run.ps1 research-r7-dossier --research-identity <ID>
                                                生成待人工审查的 R7 档案

日常启动不需要填写任何参数，直接运行 .\run.ps1 即可。

高级配置（一般无需设置）:
  TRADER_CONFIG=C:\absolute\path\runtime.json
  TRADER_HOST=127.0.0.1
  TRADER_PORT=5000
  DEEPSEEK_API_KEY=...
  DEEPSEEK_API_KEY_FILE=C:\protected\path (default: project root .token_key)
  TUSHARE_TOKEN_FILE=C:\protected\path (default: project root .token_key)
  FORCE_INSTALL_DEPS=1
"@ | Write-Host
}

$CliModes = @(
    "validate-config",
    "performance-check",
    "research-status",
    "research-history-download",
    "research-backtest",
    "research-r6-screen",
    "research-r6-daily-screen",
    "research-r6-stability-screen",
    "research-tomorrow-p2-screen",
    "research-r7-dossier"
)

if ($Mode -in @("help", "-h", "--help")) {
    Show-Usage
    exit 0
}
$IsServerMode = $Mode -in @("serve", "app")
if (-not $IsServerMode -and $Mode -notin $CliModes) {
    [Console]::Error.WriteLine("未知命令: $Mode")
    [Console]::Error.WriteLine("日常启动直接运行: .\run.ps1")
    [Console]::Error.WriteLine("查看全部命令: .\run.ps1 help")
    exit 2
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
    $env:TRADER_HOST = "127.0.0.1"
}
if (-not $env:TRADER_PORT) {
    $env:TRADER_PORT = "5000"
}

if ($IsServerMode) {
    & $Server --config $ConfigPath
    exit $LASTEXITCODE
}
$RemainingArgs = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }
& $Cli --config $ConfigPath $Mode @RemainingArgs
exit $LASTEXITCODE
