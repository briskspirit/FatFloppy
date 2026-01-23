#Requires -Version 5.1
<#
.SYNOPSIS
    Builds FatFloppy Windows application and installer.

.DESCRIPTION
    This script:
    1. Builds the PyInstaller bundle (onedir mode)
    2. Runs Inno Setup to create the installer
    3. Outputs the installer to dist/

.PARAMETER SkipInstaller
    Skip Inno Setup installer creation (only build PyInstaller bundle)

.EXAMPLE
    .\build_windows.ps1
    .\build_windows.ps1 -SkipInstaller
#>

param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

# Navigate to project root (script is in packaging/windows/)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Get-Item "$ScriptDir\..\..").FullName
Set-Location $ProjectRoot

# Read version from _version.py
$VersionFile = Get-Content "src\fatfloppy\_version.py" -Raw
if ($VersionFile -match '__version__\s*=\s*"([^"]+)"') {
    $Version = $Matches[1]
} else {
    Write-Error "Could not read version from _version.py"
    exit 1
}

$AppName = "FatFloppy"

Write-Host ""
Write-Host "========================================"
Write-Host "Building $AppName $Version for Windows"
Write-Host "========================================"
Write-Host ""

# Check for required icon
$IcoPath = "assets\icons\fatfloppy_icon.ico"
if (-not (Test-Path $IcoPath)) {
    Write-Host "Windows icon not found at $IcoPath"
    Write-Host "Attempting to convert from PNG using Pillow..."

    # Try to convert using Python Pillow
    $ConvertScript = @"
from PIL import Image
img = Image.open('assets/icons/fatfloppy_icon.png')
img.save('assets/icons/fatfloppy_icon.ico', sizes=[(16,16), (32,32), (48,48), (64,64), (128,128), (256,256)])
print('Icon converted successfully')
"@

    try {
        python -c $ConvertScript
        if ($LASTEXITCODE -ne 0) {
            throw "Icon conversion failed"
        }
    } catch {
        Write-Error "Failed to convert icon. Please install Pillow (pip install Pillow) or provide fatfloppy_icon.ico"
        exit 1
    }
}

# Clean previous builds
Write-Host "Cleaning previous builds..."
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

# Run PyInstaller
Write-Host ""
Write-Host "Running PyInstaller..."
python -m PyInstaller "packaging\windows\FatFloppy.spec"

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed"
    exit 1
}

# Verify build succeeded
if (-not (Test-Path "dist\$AppName\$AppName.exe")) {
    Write-Error "PyInstaller build failed - $AppName.exe not found"
    exit 1
}

Write-Host ""
Write-Host "PyInstaller build complete!"

# Get bundle size
$BundleSize = "{0:N2} MB" -f ((Get-ChildItem -Recurse "dist\$AppName" | Measure-Object -Property Length -Sum).Sum / 1MB)
Write-Host "Bundle size: $BundleSize"

# Create installer unless skipped
if (-not $SkipInstaller) {
    Write-Host ""
    Write-Host "Creating installer with Inno Setup..."

    # Find Inno Setup compiler
    $IsccPaths = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )

    $Iscc = $null
    foreach ($path in $IsccPaths) {
        if (Test-Path $path) {
            $Iscc = $path
            break
        }
    }

    if (-not $Iscc) {
        Write-Warning "Inno Setup not found. Please install from https://jrsoftware.org/isdown.php"
        Write-Warning "Skipping installer creation."
    } else {
        # Run Inno Setup
        & $Iscc "packaging\windows\installer.iss" /DMyAppVersion=$Version

        if ($LASTEXITCODE -ne 0) {
            Write-Error "Inno Setup failed"
            exit 1
        }

        # Get installer size
        $InstallerPath = "dist\$AppName-$Version-Windows-Setup.exe"
        if (Test-Path $InstallerPath) {
            $InstallerSize = "{0:N2} MB" -f ((Get-Item $InstallerPath).Length / 1MB)
            Write-Host ""
            Write-Host "========================================"
            Write-Host "Build complete!"
            Write-Host "========================================"
            Write-Host "Installer: $InstallerPath ($InstallerSize)"
            Write-Host "Bundle:    dist\$AppName\ ($BundleSize)"
            Write-Host ""
            Write-Host "The installer supports Windows 10+ (64-bit)."
            Write-Host "========================================"
        }
    }
} else {
    Write-Host ""
    Write-Host "========================================"
    Write-Host "Build complete! (installer skipped)"
    Write-Host "========================================"
    Write-Host "Bundle: dist\$AppName\ ($BundleSize)"
    Write-Host ""
    Write-Host "Run the application: dist\$AppName\$AppName.exe"
    Write-Host "========================================"
}
