#!/usr/bin/env pwsh
# Freshness Function App build. Adapted copy of digiassist-agent/funcbuild.ps1:
# default zip freshness.zip, Python 3.11, no config.yaml/logging.yaml/functions dir.
[CmdletBinding()]
param(
    [string]$OutputZip = "freshness.zip",
    [string]$PythonVersion = "3.11"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LibsDir = Join-Path $RootDir "libs"

Write-Host "========================================"
Write-Host " RLG Freshness Function App - Build Script"
Write-Host "========================================"

Write-Host ""
Write-Host "[1/5] Installing dependencies into ./libs ..."

$libsPopulated = (Test-Path $LibsDir) -and `
    ((Get-ChildItem -Path $LibsDir -Force -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0)

if ($libsPopulated) {
    Write-Host "  libs/ already populated, skipping dependency install (delete libs/ to rebuild)."
}
else {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $PipCmd = @("uv", "pip", "install")
        $PlatformArgs = @(
            "--python-platform", "x86_64-manylinux2014",
            "--python-version", $PythonVersion,
            "--only-binary=:all:"
        )
    }
    elseif (Get-Command pip3 -ErrorAction SilentlyContinue) {
        $PipCmd = @("pip3", "install")
        $PlatformArgs = @(
            "--platform", "manylinux2014_x86_64",
            "--python-version", $PythonVersion,
            "--only-binary=:all:"
        )
    }
    elseif (Get-Command pip -ErrorAction SilentlyContinue) {
        $PipCmd = @("pip", "install")
        $PlatformArgs = @(
            "--platform", "manylinux2014_x86_64",
            "--python-version", $PythonVersion,
            "--only-binary=:all:"
        )
    }
    else {
        Write-Error "ERROR: no uv, pip3, or pip found on PATH"
        exit 1
    }

    Write-Host "  Using: $($PipCmd -join ' ')"

    $requirements = Join-Path $RootDir "requirements.txt"
    $exe = $PipCmd[0]
    [array]$exeArgs = $PipCmd[1..($PipCmd.Count - 1)]
    & $exe @exeArgs -r $requirements --target $LibsDir @PlatformArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "ERROR: dependency install failed, removing partial libs/"
        Remove-Item -Recurse -Force $LibsDir -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host "  Dependencies installed."
}

Write-Host ""
Write-Host "[2/5] Cleaning cache from libs/ ..."

$BinDir = Join-Path $LibsDir "bin"
if (Test-Path $BinDir) {
    Remove-Item -Recurse -Force $BinDir
    Write-Host "  Removed libs/bin"
}

Get-ChildItem -Path $LibsDir -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  Removed __pycache__ directories from libs/"

Write-Host ""
Write-Host "[3/5] Cleaning cache from src/ ..."

$SrcDir = Join-Path $RootDir "src"
Get-ChildItem -Path $SrcDir -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  Removed __pycache__ directories from src/"

Write-Host ""
Write-Host "[4/5] Removing stale zip ..."

$ZipPath = Join-Path $RootDir $OutputZip
if (Test-Path $ZipPath) {
    Remove-Item -Force $ZipPath
    Write-Host "  Removed existing $OutputZip"
}

Write-Host ""
Write-Host "[5/5] Packaging files into $OutputZip ..."

$ItemsToZip = @(
    "function_app.py",
    "host.json",
    "src",
    "libs"
)

Set-Location $RootDir

foreach ($item in $ItemsToZip) {
    if (-not (Test-Path (Join-Path $RootDir $item))) {
        Write-Error "ERROR: Required item not found: $item"
        exit 1
    }
}

# Stage the payload in a temp directory so we can exclude __pycache__, *.pyc and
# log/ contents, then compress the staged tree (preserves directory structure and
# uses only cmdlets, so it works under ConstrainedLanguage mode too).
$TempRoot = if ($env:TEMP) { $env:TEMP } elseif ($env:TMPDIR) { $env:TMPDIR } else { "/tmp" }
$StageDir = Join-Path $TempRoot ("funcbuild_" + (New-Guid))
New-Item -ItemType Directory -Path $StageDir | Out-Null
try {
    foreach ($item in $ItemsToZip) {
        $source = Join-Path $RootDir $item
        if (Test-Path $source -PathType Container) {
            Copy-Item -Path $source -Destination $StageDir -Recurse -Force
        }
        else {
            Copy-Item -Path $source -Destination $StageDir -Force
        }
    }

    # Prune excluded content from the staged copy.
    Get-ChildItem -Path $StageDir -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $StageDir -File -Recurse -Filter "*.pyc" -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $StageDir -Directory -Recurse -Filter "log" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    # Safety: never ship secrets or local test data
    foreach ($bad in @(".env", "local.settings.json")) {
        Get-ChildItem -Path $StageDir -File -Recurse -Filter $bad -Force -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }

    Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $ZipPath -Force
}
finally {
    Remove-Item -Recurse -Force $StageDir -ErrorAction SilentlyContinue
}

Write-Host "  Packaged."

Write-Host ""
Write-Host "========================================"
Write-Host " Build complete: $OutputZip"
$sizeMB = "{0:N2}" -f ((Get-Item $ZipPath).Length / 1MB)
Write-Host " Size: $sizeMB MB"
Write-Host "========================================"
