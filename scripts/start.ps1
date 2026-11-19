<#
.SYNOPSIS
    Ollama Voice Input — Windows PowerShell 启动脚本
.DESCRIPTION
    自动创建/激活 .venv、同步本地 Root CA 与 <=397 天 SAN TLS 证书、启动独立 Ollama (127.0.0.1:17947)、
    Qwen3-ASR sidecar (--backend auto, 127.0.0.1:17946)、Uvicorn HTTPS Web 服务 (0.0.0.0:17945) 与
    PySide6 跨平台桌面守护进程 daemon.py，随后在默认浏览器打开方案 C 翻译主页 https://127.0.0.1:17945/。
.EXAMPLE
    .\scripts\start.ps1
    .\scripts\start.ps1 -Acceptance
#>
[CmdletBinding()]
param(
    [switch]$Acceptance
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvDir    = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonW = Join-Path $VenvDir "Scripts\pythonw.exe"
$VenvPip    = Join-Path $VenvDir "Scripts\pip.exe"
$LogDir     = Join-Path $ProjectDir ".cache\logs"
$CertDir    = Join-Path $ProjectDir "certs"
$RuntimeDir = Join-Path $ProjectDir ".cache\v3-feasibility"
$AsrModelDir = Join-Path $RuntimeDir "models\Qwen3-ASR-1.7B"
$OllamaModels = Join-Path $RuntimeDir "ollama-models"
$HfHome     = Join-Path $RuntimeDir "hf-home"

function Test-TcpPortOpen {
    param([string]$HostAddr, [int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($HostAddr, $Port, $null, $null)
        $connected = $async.AsyncWaitHandle.WaitOne(300, $false)
        if ($connected -and $client.Connected) {
            $client.EndConnect($async)
            $client.Close()
            return $true
        }
        $client.Close()
        return $false
    } catch {
        return $false
    }
}

function Quote-WinArg {
    param([string]$Arg)
    if ($Arg -match '[\s"]') {
        return '"' + ($Arg -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
    }
    return $Arg
}

Write-Host "========================================" -ForegroundColor Green
Write-Host "  Ollama Voice Input (Windows) 启动脚本" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green

# 0. 生产门闩检查（fail-closed，先于依赖安装与拉起进程；整段 try/finally 保证非 -Acceptance 模式绝不透传旧 OVI_ACCEPTANCE_RUN 给子进程）
$BootstrapPython = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
$prevAcceptance = $env:OVI_ACCEPTANCE_RUN
try {
    if ($Acceptance) {
        $env:OVI_ACCEPTANCE_RUN = "1"
        Write-Host "  验收运行：官方/真机门闩仍未关闭，状态与设置页会如实显示未达项。" -ForegroundColor Yellow
    } else {
        Remove-Item Env:OVI_ACCEPTANCE_RUN -ErrorAction SilentlyContinue
    }
    & $BootstrapPython (Join-Path $ProjectDir "_activation.py") check | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  生产门闩未关闭，未启动任何服务（验收请使用 .\scripts\start.ps1 -Acceptance）。" -ForegroundColor Red
        exit $LASTEXITCODE
    }

    New-Item -ItemType Directory -Force -Path $LogDir, $CertDir, $OllamaModels, $HfHome | Out-Null

    # 1. 创建 / 检查虚拟环境与依赖
    if (-not (Test-Path $VenvPython)) {
        Write-Host "[1/5] 创建 Python 虚拟环境..." -ForegroundColor Yellow
        & python -m venv $VenvDir
    }
    Write-Host "[1/5] 检查 Python 依赖..." -ForegroundColor Yellow
    & $VenvPip install -q -r (Join-Path $ProjectDir "requirements.txt")

    # 2. 生成 / 同步本地 Root CA 与 <=397 天全网卡 SAN 证书（单一来源 _platform.certs）
    Write-Host "[2/5] 同步本地 TLS 证书 (Root CA + <=397天 SAN)..." -ForegroundColor Yellow
    Push-Location $ProjectDir
    try {
        & $VenvPython -m _platform.certs --cert-dir $CertDir
    } finally {
        Pop-Location
    }

    # 3. 启动独立本地 Ollama（127.0.0.1:17947）
    Write-Host "[3/5] 检查本地 Ollama (127.0.0.1:17947)..." -ForegroundColor Yellow
    if (Test-TcpPortOpen -HostAddr "127.0.0.1" -Port 17947) {
        Write-Host "  本地 Ollama (17947) 已在运行 ✓" -ForegroundColor Green
    } else {
        $ProjectOllama = Join-Path $RuntimeDir "ollama-bin-0.40.16\ollama.exe"
        $OllamaBin = if (Test-Path $ProjectOllama) { $ProjectOllama } else { (Get-Command ollama -ErrorAction SilentlyContinue).Source }
        if ($OllamaBin) {
            $env:OLLAMA_HOST = "127.0.0.1:17947"
            $env:OLLAMA_MODELS = $OllamaModels
            Start-Process -FilePath $OllamaBin -ArgumentList "serve" `
                -WorkingDirectory $ProjectDir `
                -RedirectStandardOutput (Join-Path $LogDir "ollama.out.log") `
                -RedirectStandardError (Join-Path $LogDir "ollama.err.log") `
                -WindowStyle Hidden
            Write-Host "  本地 Ollama (127.0.0.1:17947) 已启动 ✓" -ForegroundColor Green
        } else {
            Write-Host "  未检测到 Ollama 可执行文件，请在设置页完成安装: https://127.0.0.1:17945/setup" -ForegroundColor Yellow
        }
    }

    # 4. 启动 Qwen3-ASR sidecar（127.0.0.1:17946，--backend auto 自动选择 CUDA/CPU）
    Write-Host "[4/5] 检查 Qwen3-ASR sidecar (127.0.0.1:17946)..." -ForegroundColor Yellow
    if (Test-TcpPortOpen -HostAddr "127.0.0.1" -Port 17946) {
        Write-Host "  Qwen3-ASR sidecar (17946) 已在运行 ✓" -ForegroundColor Green
    } elseif (Test-Path (Join-Path $AsrModelDir "config.json")) {
        $AsrPython = Join-Path $RuntimeDir "venv-asr\Scripts\python.exe"
        if (-not (Test-Path $AsrPython)) { $AsrPython = $VenvPython }
        $env:HF_HOME = $HfHome
        $env:HF_HUB_OFFLINE = "1"
        $env:PYTHONUNBUFFERED = "1"
        $AsrArgs = @(
            "-m", "runtimes.asr", "serve",
            "--host", "127.0.0.1",
            "--port", "17946",
            "--backend", "auto",
            "--gpu-memory-utilization", "0.45",
            "--max-model-len", "4096",
            "--load", "--warmup",
            "--model", $AsrModelDir
        ) | ForEach-Object { Quote-WinArg $_ }
        Start-Process -FilePath $AsrPython -ArgumentList $AsrArgs `
            -WorkingDirectory $ProjectDir `
            -RedirectStandardOutput (Join-Path $LogDir "asr.out.log") `
            -RedirectStandardError (Join-Path $LogDir "asr.err.log") `
            -WindowStyle Hidden
        Write-Host "  Qwen3-ASR sidecar 已启动 ✓（模型加载中，可在设置页查看就绪状态）" -ForegroundColor Green
    } else {
        Write-Host "  Qwen3-ASR 权重未下载，请在设置页完成下载: https://127.0.0.1:17945/setup" -ForegroundColor Yellow
    }

    # 5. 启动 Uvicorn HTTPS Web 服务 (17945) 与 PySide6 桌面守护进程
    Write-Host "[5/5] 启动 Web 服务 (17945) 与跨平台守护进程..." -ForegroundColor Yellow
    if (-not (Test-TcpPortOpen -HostAddr "127.0.0.1" -Port 17945)) {
        $ServerArgs = @(
            "-m", "uvicorn",
            "--factory", "app:create_production_app",
            "--host", "0.0.0.0",
            "--port", "17945",
            "--ssl-keyfile", (Join-Path $CertDir "key.pem"),
            "--ssl-certfile", (Join-Path $CertDir "cert.pem")
        ) | ForEach-Object { Quote-WinArg $_ }
        Start-Process -FilePath $VenvPython -ArgumentList $ServerArgs `
            -WorkingDirectory $ProjectDir `
            -RedirectStandardOutput (Join-Path $LogDir "server.out.log") `
            -RedirectStandardError (Join-Path $LogDir "server.err.log") `
            -WindowStyle Hidden
        for ($i = 0; $i -lt 30; $i++) {
            if (Test-TcpPortOpen -HostAddr "127.0.0.1" -Port 17945) { break }
            Start-Sleep -Milliseconds 300
        }
    }

    $DaemonBin = if (Test-Path $VenvPythonW) { $VenvPythonW } else { $VenvPython }
    Start-Process -FilePath $DaemonBin -ArgumentList (Quote-WinArg (Join-Path $ProjectDir "daemon.py")) `
        -WorkingDirectory $ProjectDir `
        -WindowStyle Hidden
} finally {
    if ($null -ne $prevAcceptance) {
        $env:OVI_ACCEPTANCE_RUN = $prevAcceptance
    } else {
        Remove-Item Env:OVI_ACCEPTANCE_RUN -ErrorAction SilentlyContinue
    }
}

Start-Process "https://127.0.0.1:17945/"

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  服务已就绪！" -ForegroundColor Green
Write-Host "  翻译主页: https://127.0.0.1:17945/" -ForegroundColor Green
Write-Host "  设置页面: https://127.0.0.1:17945/setup" -ForegroundColor Green
Write-Host "  Ctrl+Alt+Z   按住说话，松开自动输入" -ForegroundColor Green
Write-Host "  Ctrl+Alt+X   截屏原位翻译（红色十字框选）" -ForegroundColor Green
Write-Host "  Ctrl+Alt+O   纯 OCR 识别自动复制" -ForegroundColor Green
Write-Host "  Ctrl+Alt+R   重复输入上一次文本" -ForegroundColor Green
Write-Host "  日志目录:   $LogDir" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
