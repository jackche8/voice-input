$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Root

$Version = (& python -c "from _version import APP_VERSION; print(APP_VERSION)").Trim()
if (-not $Version) {
    throw "Failed to read APP_VERSION from _version.py"
}

Write-Host "Building Ollama Voice Input v$Version for Windows x64 (--onedir)..."
& python -m PyInstaller --clean --noconfirm "packaging\pyinstaller\ollama-voice-input.spec"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$ExePath = Join-Path $Root "dist\ollama-voice-input\ollama-voice-input.exe"
$AsrServer = Join-Path $Root "dist\ollama-voice-input\_internal\_asr_runtime\runtimes\asr\server.py"
if (-not (Test-Path $ExePath)) {
    throw "Missing expected frozen executable: $ExePath"
}
if (-not (Test-Path $AsrServer)) {
    throw "Missing isolated ASR runtime closure: $AsrServer"
}

$Iscc = Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if (-not $Iscc) {
    foreach ($Candidate in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path $Candidate) {
            $Iscc = $Candidate
            break
        }
    }
}
if (-not $Iscc) {
    throw "Inno Setup compiler (ISCC.exe) not found in PATH or Program Files."
}

Write-Host "Compiling Inno Setup installer..."
& $Iscc "/DMyAppVersion=$Version" "packaging\windows\installer.iss"
if ($LASTEXITCODE -ne 0) {
    throw "ISCC failed with exit code $LASTEXITCODE"
}

$Installer = Join-Path $Root "dist\Ollama-Voice-Input-$Version-windows-x64-setup.exe"
if (-not (Test-Path $Installer)) {
    throw "Expected installer was not produced: $Installer"
}
Write-Host "Built installer: $Installer"
