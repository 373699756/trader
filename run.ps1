Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSCommandPath
$Mode = ""
$ForwardArgs = @()

function Show-Usage {
    @"
本地 A 股研究看板

日常使用（不做离线研究）:
  .\run.ps1                         同时启动全部档位 V1、V2、V3、V5
  .\run.ps1 v2                      只启动 V2
  .\run.ps1 v1,v2,v3                同时启动所列档位，Web按钮互斥切换
  .\run.ps1 help                    查看本帮助

离线研究（仅在明确执行研究任务时使用）:
  .\run.ps1 sync                    依次更新参考、完整历史、行情窗口与公司资料
  .\run.ps1 sync history            单独更新完整历史，自动续传
  .\run.ps1 sync market             单独更新行情窗口
  .\run.ps1 sync company            单独更新公司资料
  .\run.ps1 train                   历史维护后处理全部档位 V1、V2、V3、V5
  .\run.ps1 train v1,v5             历史维护后只训练所列档位

档位以逗号分隔，可自由组合；省略时选择全部。V1为工程Shadow，V5需可选CPU依赖。
本地检测使用 scripts/diagnose_runtime.py --profile local。

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

if ($args.Count -gt 0 -and [string]$args[0] -in $PublicModes) {
    $Mode = [string]$args[0]
    $ForwardArgs = @($args | Select-Object -Skip 1)
} else {
    $ForwardArgs = @($args)
}

if ($Mode -in @("help", "-h", "--help")) {
    Show-Usage
    exit 0
}
$IsServerMode = [string]::IsNullOrEmpty($Mode)
if ($Mode -eq "sync") {
    if ($ForwardArgs.Count -gt 1 -or ($ForwardArgs.Count -eq 1 -and $ForwardArgs[0] -notin @("history", "market", "company"))) {
        [Console]::Error.WriteLine("用法：.\run.ps1 sync [history|market|company]")
        exit 2
    }
} elseif ($ForwardArgs.Count -gt 0) {
    # PowerShell may pass an unquoted comma list as an array; normalize it once.
    $Selection = ($ForwardArgs | ForEach-Object { $_ -join "," }) -join ","
    if ($Selection -cnotmatch '^v(1|2|3|5)(,v(1|2|3|5))*$') {
        [Console]::Error.WriteLine("档位列表只能包含 v1、v2、v3、v5，以逗号分隔且不得重复。")
        exit 2
    }
    $Profiles = $Selection.Split(",")
    if (@($Profiles | Select-Object -Unique).Count -ne $Profiles.Count) {
        [Console]::Error.WriteLine("档位不能重复。")
        exit 2
    }
    $ForwardArgs = @($Selection)
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
if ($IsServerMode) {
    & $SelectedEntryPoint --config $ConfigPath @ForwardArgs
    exit $LASTEXITCODE
}
& $SelectedEntryPoint --config $ConfigPath $Mode @ForwardArgs
exit $LASTEXITCODE
