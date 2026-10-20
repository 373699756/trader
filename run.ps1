Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSCommandPath
$Mode = ""
$ModeSet = $false
$ScoringProfile = $null
$ScoringProfileSet = $false
$ForwardArgs = @()

function Show-Usage {
    @"
本地 A 股研究看板

日常使用（不做离线研究）:
  .\run.ps1                         以默认 V2 启动并加载共享双头模型
  .\run.ps1 --profile v2|v3         显式使用 V2 或 V3 启动
  .\run.ps1 help                    查看本帮助

离线研究（仅在明确执行研究任务时使用）:
  .\run.ps1 sync                    依次更新参考、完整历史、行情窗口与公司资料
  .\run.ps1 sync history            单独更新完整历史，自动续传
  .\run.ps1 sync market             单独更新行情窗口
  .\run.ps1 sync company            单独更新公司资料
  .\run.ps1 train                   历史维护后联合训练或复用 V2/V3

看板可追加 --profile v2|v3|both；本地检测使用 scripts/diagnose_runtime.py --profile local。

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

$PublicModes = @("help", "-h", "--help", "sync", "train")

for ($Index = 0; $Index -lt $args.Count; $Index++) {
    $Argument = [string]$args[$Index]
    if ($Argument -eq "--profile") {
        if ($Index + 1 -ge $args.Count) {
            [Console]::Error.WriteLine("缺少 --profile 的值（v2 或 v3）。")
            exit 2
        }
        $Index++
        $ScoringProfile = [string]$args[$Index]
        $ScoringProfileSet = $true
    }
    elseif ($Argument.StartsWith("--profile=")) {
        $ScoringProfile = $Argument.Substring("--profile=".Length)
        $ScoringProfileSet = $true
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

if ($ScoringProfileSet -and $ScoringProfile -notin @("v2", "v3", "both")) {
    [Console]::Error.WriteLine("评分档位只能是 v2 或 v3: $ScoringProfile")
    exit 2
}
if (($Mode -in @("sync", "train") -and $ScoringProfileSet) -or ($Mode -eq "train" -and $ForwardArgs.Count -gt 0)) {
    [Console]::Error.WriteLine("$Mode 不接受任何参数；请只运行 .\run.ps1 $Mode。")
    exit 2
}

if ($Mode -eq "sync" -and $ForwardArgs.Count -gt 0) {
    if ($ForwardArgs.Count -ne 1 -or $ForwardArgs[0] -notin @("history", "market", "company")) {
        [Console]::Error.WriteLine("用法：.\run.ps1 sync [history|market|company]")
        exit 2
    }
}

if ($Mode -in @("help", "-h", "--help")) {
    Show-Usage
    exit 0
}
$IsServerMode = [string]::IsNullOrEmpty($Mode)
if (-not $IsServerMode -and $Mode -notin @("sync", "train")) {
    [Console]::Error.WriteLine("未知命令: $Mode")
    [Console]::Error.WriteLine("日常启动直接运行: .\run.ps1")
    [Console]::Error.WriteLine("查看全部命令: .\run.ps1 help")
    exit 2
}

$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { Join-Path $RootDir ".venv" }
$IsWindowsHost = -not $PSVersionTable.ContainsKey("Platform") -or $PSVersionTable.Platform -eq "Win32NT"
$VenvPython = if ($IsWindowsHost) { Join-Path $VenvDir "Scripts\python.exe" } else { Join-Path $VenvDir "bin/python3" }
$Server = if ($IsWindowsHost) { Join-Path $VenvDir "Scripts\trader-server.exe" } else { Join-Path $VenvDir "bin/trader-server" }
$Cli = if ($IsWindowsHost) { Join-Path $VenvDir "Scripts\trader-cli.exe" } else { Join-Path $VenvDir "bin/trader-cli" }
$SelectedEntryPoint = if ($IsServerMode) { $Server } else { $Cli }
$ConfigPath = if ($env:TRADER_CONFIG) { $env:TRADER_CONFIG } else { Join-Path $RootDir "config\runtime.json" }

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
    & $VenvPython -m pip install --disable-pip-version-check -e $RootDir | ForEach-Object { [Console]::Error.WriteLine($_) }
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
$ProfileArgs = if ($ScoringProfileSet) { @("--profile", $ScoringProfile) } else { @() }

if ($IsServerMode) {
    & $SelectedEntryPoint --config $ConfigPath @ProfileArgs @ForwardArgs
    exit $LASTEXITCODE
}
& $SelectedEntryPoint --config $ConfigPath @ProfileArgs $Mode @ForwardArgs
exit $LASTEXITCODE
