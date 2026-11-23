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

# ---- Bundle Tesseract 5 + official tessdata_fast 4.1.0 (chi_sim/chi_tra/eng, SHA256 verified) ----
function Test-RealTesseractDir {
    param([string]$Dir)
    if (-not $Dir -or -not (Test-Path $Dir -PathType Container)) {
        return $false
    }
    if ($Dir -match '(?i)(^|\\)chocolatey\\bin(\\|$)') {
        return $false
    }
    if (-not (Test-Path (Join-Path $Dir "tesseract.exe") -PathType Leaf)) {
        return $false
    }
    $HasTessDll = @(Get-ChildItem -Path $Dir -Filter "*tesseract*.dll" -File -ErrorAction SilentlyContinue).Count -gt 0
    $HasLeptDll = @(Get-ChildItem -Path $Dir -Filter "*lept*.dll" -File -ErrorAction SilentlyContinue).Count -gt 0
    return ($HasTessDll -or $HasLeptDll)
}

$CandidateDirs = @()
if ($env:TESSERACT_DIR) {
    $CandidateDirs += $env:TESSERACT_DIR
}
$CandidateDirs += @(
    "${env:ProgramFiles}\Tesseract-OCR",
    "${env:ProgramFiles(x86)}\Tesseract-OCR"
)
$TessCmd = Get-Command tesseract -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if ($TessCmd) {
    $CandidateDirs += (Split-Path -Parent $TessCmd)
}

$TesseractDir = $null
foreach ($CandidateDir in $CandidateDirs) {
    if (Test-RealTesseractDir -Dir $CandidateDir) {
        $TesseractDir = $CandidateDir
        break
    }
}
if (-not $TesseractDir) {
    throw "Real Tesseract 5 installation directory not found (checked TESSERACT_DIR, Program Files\Tesseract-OCR, PATH; rejected chocolatey\bin shims)."
}

$SrcTesseractExe = Join-Path $TesseractDir "tesseract.exe"
$TessVersionText = (& $SrcTesseractExe --version 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0 -or $TessVersionText -notmatch "tesseract\s+v?5\.") {
    throw "Expected Tesseract 5 at $SrcTesseractExe, got: $TessVersionText"
}

$BundledTessDir = Join-Path $Root "dist\ollama-voice-input\_internal\tesseract"
$BundledTessdataDir = Join-Path $BundledTessDir "tessdata"
if (Test-Path $BundledTessDir) {
    Remove-Item -Recurse -Force $BundledTessDir
}
New-Item -ItemType Directory -Force -Path $BundledTessdataDir | Out-Null

Write-Host "Staging bundled Tesseract 5 from $TesseractDir into $BundledTessDir..."
Copy-Item -Force -Path $SrcTesseractExe -Destination (Join-Path $BundledTessDir "tesseract.exe")
Get-ChildItem -Path $TesseractDir -Filter "*.dll" -File | ForEach-Object {
    Copy-Item -Force -Path $_.FullName -Destination (Join-Path $BundledTessDir $_.Name)
}
$BundledExe = Join-Path $BundledTessDir "tesseract.exe"
$BundledTessDlls = @(Get-ChildItem -Path $BundledTessDir -Filter "*tesseract*.dll" -File -ErrorAction SilentlyContinue)
$BundledLeptDlls = @(Get-ChildItem -Path $BundledTessDir -Filter "*lept*.dll" -File -ErrorAction SilentlyContinue)
if (-not (Test-Path $BundledExe -PathType Leaf)) {
    throw "Staged Tesseract bundle missing tesseract.exe: $BundledTessDir"
}
if ($BundledTessDlls.Count -lt 1) {
    throw "Staged Tesseract bundle missing *tesseract*.dll runtime library in $BundledTessDir"
}
if ($BundledLeptDlls.Count -lt 1) {
    throw "Staged Tesseract bundle missing *lept*.dll runtime library in $BundledTessDir"
}

$SrcConfigsDir = Join-Path $TesseractDir "tessdata\configs"
if (Test-Path $SrcConfigsDir) {
    Copy-Item -Recurse -Force -Path $SrcConfigsDir -Destination (Join-Path $BundledTessdataDir "configs")
}

$TessdataBaseUrl = "https://github.com/tesseract-ocr/tessdata_fast/raw/4.1.0"
$TessdataHashes = [ordered]@{
    "eng.traineddata"     = "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
    "chi_sim.traineddata" = "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730"
    "chi_tra.traineddata" = "529c5b5797d64b126065cd55f2bb4c7fd7b15790798091b1ff259941a829330b"
}
foreach ($Entry in $TessdataHashes.GetEnumerator()) {
    $FileName = $Entry.Key
    $ExpectedHash = $Entry.Value
    $DestFile = Join-Path $BundledTessdataDir $FileName
    $Url = "$TessdataBaseUrl/$FileName"
    Write-Host "Downloading $Url ..."
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $DestFile
    $ActualHash = (Get-FileHash -Path $DestFile -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualHash -ne $ExpectedHash) {
        Remove-Item -Force -ErrorAction SilentlyContinue $DestFile
        throw "SHA256 mismatch for ${FileName}: expected $ExpectedHash, got $ActualHash"
    }
}

# ---- Spaced-path isolated OCR smoke test (strip system Tesseract & chocolatey\bin from PATH; clear TESSDATA_PREFIX) ----
$SmokeRoot = Join-Path $Root ".pp\tmp\Ollama Voice Input"
$SmokeInternal = Join-Path $SmokeRoot "_internal"
$SmokeTessDir = Join-Path $SmokeInternal "tesseract"
$SmokeTessExe = Join-Path $SmokeTessDir "tesseract.exe"
$SmokeTessdata = Join-Path $SmokeTessDir "tessdata"
$SamplePng = Join-Path $Root "docs\site\assets\workbench-light.png"
if (-not (Test-Path $SamplePng)) {
    throw "Missing OCR smoke sample image: $SamplePng"
}
if (Test-Path $SmokeRoot) {
    Remove-Item -Recurse -Force $SmokeRoot
}
New-Item -ItemType Directory -Force -Path $SmokeInternal | Out-Null
Copy-Item -Recurse -Force -Path $BundledTessDir -Destination $SmokeTessDir

$OrigPath = $env:PATH
$OrigTessdata = $env:TESSDATA_PREFIX
try {
    $NormalizedSrcDir = $TesseractDir.TrimEnd("\").ToLowerInvariant()
    $FilteredPath = ($OrigPath -split ";" | Where-Object {
        $p = $_.Trim()
        $p -and ($p.TrimEnd("\").ToLowerInvariant() -ne $NormalizedSrcDir) -and ($p -notmatch "(?i)tesseract") -and ($p -notmatch "(?i)chocolatey\\bin")
    }) -join ";"
    $env:PATH = $FilteredPath
    Remove-Item Env:TESSDATA_PREFIX -ErrorAction SilentlyContinue

    Write-Host "Running spaced-path isolated Tesseract smoke test in $SmokeTessDir..."
    $env:TESSDATA_PREFIX = $SmokeTessdata
    $ListLangsOut = (& $SmokeTessExe --list-langs 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "Bundled tesseract --list-langs failed (exit $LASTEXITCODE): $ListLangsOut"
    }
    foreach ($Lang in @("eng", "chi_sim", "chi_tra")) {
        if ($ListLangsOut -notmatch "(?m)^\s*$Lang\s*$") {
            throw "Bundled tesseract --list-langs missing ${Lang}: $ListLangsOut"
        }
    }

    # Verify Python _ocr.image_to_text & _ocr_layout.extract_ocr_layout with no external TESSDATA_PREFIX
    Remove-Item Env:TESSDATA_PREFIX -ErrorAction SilentlyContinue
    $PySmoke = @'
import os, re, sys
from pathlib import Path
import _ocr
import _ocr_layout

smoke_internal = Path(sys.argv[1]).resolve()
sample_png = Path(sys.argv[2]).resolve()
_ocr.PROGRAM_ROOT = smoke_internal
_ocr.clear_language_cache()

cmd, td = _ocr.resolve_tesseract()
assert Path(cmd).resolve() == (smoke_internal / "tesseract" / "tesseract.exe").resolve(), (cmd, td)
assert td is not None and td.resolve() == (smoke_internal / "tesseract" / "tessdata").resolve(), td

langs = _ocr.list_tesseract_languages()
for code in ("chi_sim", "chi_tra", "eng"):
    assert code in langs, langs

png_bytes = sample_png.read_bytes()
text = _ocr.image_to_text(png_bytes)
assert re.search(r"[\u4e00-\u9fff]", text), f"missing CJK in image_to_text output: {text[:120]!r}"
assert re.search(r"[A-Za-z]{2,}", text), f"missing English in image_to_text output: {text[:120]!r}"
assert "TESSDATA_PREFIX" not in os.environ, "TESSDATA_PREFIX leaked after image_to_text"

_w, _h, layout_text, blocks = _ocr_layout.extract_ocr_layout(png_bytes)
assert len(blocks) > 0, "extract_ocr_layout returned empty blocks"
assert re.search(r"[\u4e00-\u9fff]", layout_text), f"missing CJK in layout output: {layout_text[:120]!r}"
assert re.search(r"[A-Za-z]{2,}", layout_text), f"missing English in layout output: {layout_text[:120]!r}"
assert "TESSDATA_PREFIX" not in os.environ, "TESSDATA_PREFIX leaked after extract_ocr_layout"
print(f"Windows bundled OCR smoke OK: langs={langs}, text_chars={len(text)}, blocks={len(blocks)}")
'@
    & python -c $PySmoke $SmokeInternal $SamplePng
    if ($LASTEXITCODE -ne 0) {
        throw "Python _ocr/_ocr_layout spaced-path smoke failed with exit code $LASTEXITCODE"
    }
} finally {
    $env:PATH = $OrigPath
    if ($null -ne $OrigTessdata) {
        $env:TESSDATA_PREFIX = $OrigTessdata
    } else {
        Remove-Item Env:TESSDATA_PREFIX -ErrorAction SilentlyContinue
    }
    if (Test-Path $SmokeRoot) {
        Remove-Item -Recurse -Force $SmokeRoot
    }
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
