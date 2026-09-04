Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSCommandPath
$Mode = ""
$ModeSet = $false
$ScoringProfile = "v1"
$ForwardArgs = @()

function Show-Usage {
    @"
本地 A 股研究看板

日常使用（不做离线研究）:
  .\run.ps1                         以默认 V1 启动本地 A 股研究看板
  .\run.ps1 --profile v2            显式使用 V2 启动
  .\run.ps1 check                   依次校验配置、研究状态和性能门禁
  .\run.ps1 help                    查看本帮助

离线研究（仅在明确执行研究任务时使用）:
  .\run.ps1 download_history        下载/续传 BaoStock 历史日线归档
  .\run.ps1 train-tomorrow          从封存状态推导并连续运行可用 Tomorrow 训练阶段

所有命令都可追加 --profile v1|v2|v3；未指定时为 V1。

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

$PublicModes = @("help", "-h", "--help", "check", "download_history", "train-tomorrow")

for ($Index = 0; $Index -lt $args.Count; $Index++) {
    $Argument = [string]$args[$Index]
    if ($Argument -eq "--profile") {
        if ($Index + 1 -ge $args.Count) {
            [Console]::Error.WriteLine("缺少 --profile 的值（v1 或 v2）。")
            exit 2
        }
        $Index++
        $ScoringProfile = [string]$args[$Index]
    }
    elseif ($Argument.StartsWith("--profile=")) {
        $ScoringProfile = $Argument.Substring("--profile=".Length)
    }
    elseif ($Argument -in $PublicModes -and -not $ModeSet) {
        $Mode = $Argument
        $ModeSet = $true
    }
    elseif ($ModeSet) {
        $ForwardArgs += $Argument
    }
    else {
        [Console]::Error.WriteLine("未知命令: $Argument")
        [Console]::Error.WriteLine("日常启动直接运行: .\run.ps1")
        [Console]::Error.WriteLine("查看全部命令: .\run.ps1 help")
        exit 2
    }
}

if ($ScoringProfile -notin @("v1", "v2")) {
    [Console]::Error.WriteLine("评分档位只能是 v1 或 v2: $ScoringProfile")
    exit 2
}

if ($Mode -in @("help", "-h", "--help")) {
    Show-Usage
    exit 0
}
$IsServerMode = [string]::IsNullOrEmpty($Mode)
if (-not $IsServerMode -and $Mode -notin @("check", "download_history", "train-tomorrow")) {
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
$SelectedEntryPoint = if ($IsServerMode) { $Server } else { $Cli }
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

$NeedsInstall = -not (Test-Path $SelectedEntryPoint)
if (-not $NeedsInstall) {
    $NeedsInstall = (Get-Item (Join-Path $RootDir "pyproject.toml")).LastWriteTimeUtc -gt (Get-Item $SelectedEntryPoint).LastWriteTimeUtc
}
if (-not $NeedsInstall) {
    try {
        & $SelectedEntryPoint --help *> $null
        $NeedsInstall = $LASTEXITCODE -ne 0
    }
    catch {
        $NeedsInstall = $true
    }
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
    & $SelectedEntryPoint --config $ConfigPath --profile $ScoringProfile @ForwardArgs
    exit $LASTEXITCODE
}
& $SelectedEntryPoint --config $ConfigPath --profile $ScoringProfile $Mode @ForwardArgs
exit $LASTEXITCODE
