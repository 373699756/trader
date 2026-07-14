Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSCommandPath
Set-Location $RootDir

function Show-Usage {
    @'
用法:
  .\run.ps1 [serve]
  .\run.ps1 after-close [daily_job 参数]
  run.bat [serve]

常用环境变量:
  $env:PORT="5050"; .\run.ps1
  $env:PROXY_MODE="on"; $env:PROXY_PORT="7890"; .\run.ps1
  $env:PROXY_MODE="off"; .\run.ps1
  $env:PROXY_HOST="127.0.0.1"; $env:PROXY_PORT="7890"; .\run.ps1
  $env:PROXY_SCHEME="socks5h"; $env:PROXY_PORT="1080"; .\run.ps1
  $env:INTERNET_CHECK_URLS="https://pypi.org/simple/pip/"; .\run.ps1
  $env:SKIP_PROXY_CHECK="1"; .\run.ps1
  .\run.ps1 after-close --strategy all
  .\run.ps1 after-close --market-data-limit 500

默认会自动探测 127.0.0.1、localhost、host.docker.internal 的常见代理端口。
'@ | Write-Host
}

function Get-EnvValue {
    param(
        [string]$Name,
        [string]$DefaultValue = ""
    )

    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $DefaultValue
    }
    return $value
}

function Resolve-ProjectPath {
    param([string]$PathValue)

    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return $PathValue
    }
    return (Join-Path $RootDir $PathValue)
}

function Test-IsWindowsRuntime {
    if (Get-Variable -Name IsWindows -Scope Global -ErrorAction SilentlyContinue) {
        return $IsWindows
    }
    return $true
}

function Clear-ProxyEnv {
    foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "PIP_PROXY")) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
}

function Test-TcpOpen {
    param(
        [string]$TargetHost,
        [int]$TargetPort
    )

    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(2000, $false)) {
            $client.Close()
            return $false
        }
        $client.EndConnect($async)
        $client.Close()
        return $true
    } catch {
        if ($null -ne $client) {
            $client.Close()
        }
        return $false
    }
}

function Get-CandidateHosts {
    $configured = Get-EnvValue "PROXY_HOST" ""
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        return @($configured)
    }

    $hosts = @("127.0.0.1", "localhost", "host.docker.internal")
    $seen = @{}
    $result = @()
    foreach ($candidate in $hosts) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and -not $seen.ContainsKey($candidate)) {
            $seen[$candidate] = $true
            $result += $candidate
        }
    }
    return $result
}

function Get-CandidatePorts {
    $configured = Get-EnvValue "PROXY_PORT" ""
    if (-not [string]::IsNullOrWhiteSpace($configured)) {
        return @($configured)
    }

    return @("7890", "7897", "7891", "10809", "10808", "20171", "2080", "8080", "8118", "1080")
}

function Set-ProxyEnv {
    param(
        [string]$ProxyHostName,
        [string]$ProxyPortValue
    )

    $url = "${Script:ProxyScheme}://${ProxyHostName}:$ProxyPortValue"
    foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "PIP_PROXY")) {
        [Environment]::SetEnvironmentVariable($name, $url, "Process")
    }

    $defaultNoProxy = "localhost,127.0.0.1,::1"
    if ([string]::IsNullOrWhiteSpace((Get-EnvValue "NO_PROXY" ""))) {
        [Environment]::SetEnvironmentVariable("NO_PROXY", $defaultNoProxy, "Process")
    }
    if ([string]::IsNullOrWhiteSpace((Get-EnvValue "no_proxy" ""))) {
        [Environment]::SetEnvironmentVariable("no_proxy", (Get-EnvValue "NO_PROXY" $defaultNoProxy), "Process")
    }
}

function Test-AnyProxyEnv {
    foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")) {
        if (-not [string]::IsNullOrWhiteSpace((Get-EnvValue $name ""))) {
            return $true
        }
    }
    return $false
}

function Get-FirstProxyEnv {
    foreach ($name in @("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")) {
        $value = Get-EnvValue $name ""
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value
        }
    }
    return ""
}

function Find-Proxy {
    foreach ($candidateHost in Get-CandidateHosts) {
        foreach ($candidatePort in Get-CandidatePorts) {
            if (Test-TcpOpen $candidateHost ([int]$candidatePort)) {
                return [pscustomobject]@{
                    Host = $candidateHost
                    Port = $candidatePort
                }
            }
        }
    }

    return $null
}

function Show-ProxyEnv {
    $shown = $false
    foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")) {
        $value = Get-EnvValue $name ""
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            if (-not $shown) {
                Write-Host "当前代理环境变量:"
                $shown = $true
            }
            Write-Host "  $name=$value"
        }
    }
}

function Configure-Proxy {
    if ((Get-EnvValue "CLEAR_PROXY" "0") -eq "1") {
        $Script:ProxyMode = "off"
    }

    switch ($Script:ProxyMode.ToLowerInvariant()) {
        "off" {
            Clear-ProxyEnv
            Write-Host "代理模式: off，已清理代理环境变量。"
        }
        "on" {
            $proxyHost = Get-EnvValue "PROXY_HOST" "127.0.0.1"
            $proxyPort = Get-EnvValue "PROXY_PORT" "7890"
            if (-not (Test-TcpOpen $proxyHost ([int]$proxyPort))) {
                throw "代理模式: on，但无法连接 ${proxyHost}:$proxyPort。请确认代理客户端已启动，并开启 HTTP/Mixed 代理端口；或用 PROXY_HOST/PROXY_PORT 指定。"
            }
            Set-ProxyEnv $proxyHost $proxyPort
            Write-Host "代理模式: on，使用 ${Script:ProxyScheme}://${proxyHost}:$proxyPort"
        }
        "auto" {
            if (Test-AnyProxyEnv) {
                Write-Host "代理模式: auto，沿用当前代理环境变量。"
                if ([string]::IsNullOrWhiteSpace((Get-EnvValue "PIP_PROXY" ""))) {
                    [Environment]::SetEnvironmentVariable("PIP_PROXY", (Get-FirstProxyEnv), "Process")
                }
            } else {
                $detected = Find-Proxy
                if ($null -ne $detected) {
                    Set-ProxyEnv $detected.Host $detected.Port
                    Write-Host "代理模式: auto，检测到代理 ${Script:ProxyScheme}://$($detected.Host):$($detected.Port)"
                } else {
                    Write-Host "代理模式: auto，未检测到可用代理，本次直连。"
                    Write-Host "如需强制使用代理: `$env:PROXY_MODE=`"on`"; `$env:PROXY_PORT=`"7890`"; .\run.ps1"
                }
            }
        }
        default {
            throw "PROXY_MODE 只能是 auto、on 或 off，当前为: $Script:ProxyMode"
        }
    }
}

function New-PythonCommand {
    param(
        [string]$File,
        [string[]]$PrefixArgs = @()
    )

    return [pscustomobject]@{
        File = $File
        PrefixArgs = @($PrefixArgs)
    }
}

function Invoke-PythonCapture {
    param(
        [object]$Python,
        [string[]]$Arguments
    )

    $allArgs = @($Python.PrefixArgs) + @($Arguments)
    $output = & ($Python.File) @allArgs 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令执行失败: $($Python.File) $($allArgs -join ' ')"
    }
    return ($output | Select-Object -First 1)
}

function Get-PythonVersion {
    param([object]$Python)

    $line = Invoke-PythonCapture $Python @("-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
    return ([string]$line).Trim()
}

function Test-PythonCommand {
    param([object]$Python)

    try {
        [void](Get-PythonVersion $Python)
        return $true
    } catch {
        return $false
    }
}

function Assert-PythonVersion {
    param(
        [object]$Python,
        [string]$Description
    )

    $versionText = Get-PythonVersion $Python
    $parts = $versionText.Split(".")
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    if ($major -ne 3 -or $minor -lt $Script:PythonMinMinor -or $minor -gt $Script:PythonMaxMinor) {
        throw "当前 Python 版本为 $versionText，$Description 依赖要求 Python 3.$Script:PythonMinMinor-3.$Script:PythonMaxMinor。请先安装兼容的 Python 后重试；如果已有旧虚拟环境，请删除后重建。"
    }
}

function Find-Python {
    $candidates = @()
    $embedded = Join-Path $RootDir ".runtime\python-3.11\python.exe"
    if (Test-Path $embedded) {
        $candidates += (New-PythonCommand -File $embedded)
    }

    for ($minor = $Script:PythonMaxMinor; $minor -ge $Script:PythonMinMinor; $minor--) {
        $candidates += (New-PythonCommand -File "py" -PrefixArgs @("-3.$minor"))
    }
    $candidates += (New-PythonCommand -File "python")
    $candidates += (New-PythonCommand -File "python3")

    foreach ($candidate in $candidates) {
        if (Test-PythonCommand $candidate) {
            return $candidate
        }
    }

    return $null
}

function Invoke-NativeOk {
    param(
        [string]$File,
        [string[]]$Arguments
    )

    try {
        & $File @Arguments
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Invoke-Native {
    param(
        [string]$File,
        [string[]]$Arguments
    )

    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败: $File $($Arguments -join ' ')"
    }
}

function Ensure-Venv {
    if (Test-Path $Script:VenvPython) {
        Assert-PythonVersion (New-PythonCommand -File $Script:VenvPython) "本项目"
        return
    }

    $python = Find-Python
    if ($null -eq $python) {
        throw "未找到 Python。请安装兼容版本 Python 3.$Script:PythonMinMinor-3.$Script:PythonMaxMinor。"
    }

    Assert-PythonVersion $python "本项目"
    Write-Host "创建虚拟环境: $Script:VenvDir"
    $venvArgs = @($python.PrefixArgs) + @("-m", "venv", $Script:VenvDir)
    Invoke-Native ($python.File) $venvArgs
}

function Check-InternetConnectivity {
    if ($Script:SkipProxyCheck -eq "1") {
        Write-Host "已跳过启动前外网检查。"
        return
    }

    $code = @'
import sys
import urllib.request

url = sys.argv[1]
request = urllib.request.Request(url, headers={"User-Agent": "trader-run-ps1/1.0"})
with urllib.request.urlopen(request, timeout=8) as response:
    if response.status >= 400:
        raise SystemExit(f"HTTP {response.status}")
'@

    Write-Host "启动前检查外网连通性..."
    foreach ($url in ($Script:InternetCheckUrls -split "\s+" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        Write-Host "  尝试: $url"
        if (Invoke-NativeOk $Script:VenvPython @("-c", $code, $url)) {
            Write-Host "外网检查通过。"
            return
        }
    }

    throw "外网检查失败，已停止启动。请确认代理已开启，并检查 HTTP/Mixed 端口是否正确。常见用法: `$env:PROXY_MODE=`"on`"; `$env:PROXY_PORT=`"7897`"; .\run.ps1。确认不需要检查时，可显式使用: `$env:SKIP_PROXY_CHECK=`"1`"; .\run.ps1"
}

function Install-Dependencies {
    $pipArgs = @(
        "--disable-pip-version-check",
        "--default-timeout", $Script:PipTimeout,
        "--retries", $Script:PipRetries
    )

    $pipProxy = Get-EnvValue "PIP_PROXY" ""
    if (-not [string]::IsNullOrWhiteSpace($pipProxy)) {
        $pipArgs += @("--proxy", $pipProxy)
    }

    if (-not [string]::IsNullOrWhiteSpace($Script:PipOnlyBinary)) {
        $pipArgs += @("--only-binary", $Script:PipOnlyBinary)
    }

    Write-Host "安装/更新依赖..."
    Invoke-Native $Script:VenvPython (@("-m", "pip", "install") + $pipArgs + @("--upgrade", "pip"))
    Invoke-Native $Script:VenvPython (@("-m", "pip", "install") + $pipArgs + @("--upgrade", "setuptools", "wheel"))

    if (Invoke-NativeOk $Script:VenvPython (@("-m", "pip", "install") + $pipArgs + @("--prefer-binary", "-r", "requirements.txt"))) {
        return
    }

    Write-Host "首次安装失败，重试一次：关闭构建隔离后再次安装（适配 Python 3.14）。"
    if (Invoke-NativeOk $Script:VenvPython (@("-m", "pip", "install") + $pipArgs + @("--no-build-isolation", "--prefer-binary", "-r", "requirements.txt"))) {
        return
    }

    Write-Host "再次失败，尝试允许写入受限环境（外部管理环境兼容）。"
    Invoke-Native $Script:VenvPython (@("-m", "pip", "install") + $pipArgs + @("--break-system-packages", "--no-build-isolation", "--prefer-binary", "-r", "requirements.txt"))
}

try {
    $ScriptArgs = @($args)
    if ($ScriptArgs.Count -gt 0 -and ($ScriptArgs[0] -eq "-h" -or $ScriptArgs[0] -eq "--help")) {
        Show-Usage
        exit 0
    }

    $RunMode = "serve"
    if ($ScriptArgs.Count -gt 0) {
        $RunMode = $ScriptArgs[0]
    }
    $RunArgs = @()
    if ($ScriptArgs.Count -gt 1) {
        $RunArgs = $ScriptArgs[1..($ScriptArgs.Count - 1)]
    }

    switch ($RunMode) {
        "serve" {}
        "app" {}
        "after-close" {}
        "daily-job" {}
        default {
            Write-Host "未知运行模式: $RunMode"
            Write-Host ""
            Show-Usage
            exit 1
        }
    }

    $Script:VenvDir = Resolve-ProjectPath (Get-EnvValue "VENV_DIR" (Join-Path $RootDir ".venv"))
    $isWindowsRuntime = Test-IsWindowsRuntime
    $venvPythonRelative = if ($isWindowsRuntime) { "Scripts\python.exe" } else { "bin/python" }
    $Script:VenvPython = Join-Path $Script:VenvDir $venvPythonRelative
    $Script:ListenHost = Get-EnvValue "HOST" "127.0.0.1"
    $Script:Port = Get-EnvValue "PORT" "5000"
    $Script:PythonMinMinor = [int](Get-EnvValue "PYTHON_MIN_MINOR" "9")
    $Script:PythonMaxMinor = [int](Get-EnvValue "PYTHON_MAX_MINOR" "14")
    $Script:ProxyMode = Get-EnvValue "PROXY_MODE" "auto"
    $Script:ProxyScheme = Get-EnvValue "PROXY_SCHEME" "http"
    $Script:InternetCheckUrls = Get-EnvValue "INTERNET_CHECK_URLS" "https://pypi.org/simple/pip/ https://www.google.com/generate_204"
    $Script:SkipProxyCheck = Get-EnvValue "SKIP_PROXY_CHECK" "0"
    $Script:PipTimeout = Get-EnvValue "PIP_TIMEOUT" "60"
    $Script:PipRetries = Get-EnvValue "PIP_RETRIES" "5"
    $Script:PipOnlyBinary = Get-EnvValue "PIP_ONLY_BINARY" ""

    New-Item -ItemType Directory -Path (Join-Path $RootDir ".runtime") -Force | Out-Null
    Configure-Proxy
    Show-ProxyEnv
    Write-Host ""

    $venvAlreadyReady = Test-Path $Script:VenvPython
    Ensure-Venv

    if (($RunMode -eq "after-close" -or $RunMode -eq "daily-job") -and $venvAlreadyReady -and (Get-EnvValue "AFTER_CLOSE_INSTALL_DEPS" "0") -ne "1") {
        Write-Host "盘后模式: 已检测到虚拟环境，跳过依赖安装检查。"
    } else {
        Check-InternetConnectivity
        Install-Dependencies
    }

    $env:HOST = $Script:ListenHost
    $env:PORT = $Script:Port
    $env:FLASK_RUN_HOST = $Script:ListenHost
    $env:FLASK_RUN_PORT = $Script:Port

    if ($RunMode -eq "after-close" -or $RunMode -eq "daily-job") {
        if ([string]::IsNullOrWhiteSpace((Get-EnvValue "ENABLE_HISTORY_FACTORS" ""))) {
            $env:ENABLE_HISTORY_FACTORS = "1"
        }
        Write-Host ""
        Write-Host "运行盘后流水线: market_data --download -> daily_job update/factors"
        Write-Host "历史因子: ENABLE_HISTORY_FACTORS=$env:ENABLE_HISTORY_FACTORS"
        Write-Host ""
        $dailyJobArgs = @("-m", "stock_analyzer.daily_job", "--after-close") + @($RunArgs)
        & $Script:VenvPython @dailyJobArgs
        exit $LASTEXITCODE
    }

    if ([string]::IsNullOrWhiteSpace((Get-EnvValue "ENABLE_HISTORY_FACTORS" ""))) {
        $env:ENABLE_HISTORY_FACTORS = "1"
    }
    Write-Host ""
    Write-Host "启动看板: http://$($Script:ListenHost):$($Script:Port)"
    Write-Host "历史因子: ENABLE_HISTORY_FACTORS=$env:ENABLE_HISTORY_FACTORS（可显式设为 0 关闭）"
    Write-Host "按 Ctrl+C 停止。"
    Write-Host ""

    & $Script:VenvPython "app.py"
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
