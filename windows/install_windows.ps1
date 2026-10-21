<#
  AstroFlow installer for Windows 10 / 11 (64-bit).

  What it does:
    1. copies AstroFlow into a folder (default: your user's Programs folder - no administrator rights needed)
    2. downloads the official "embeddable" Python from python.org into that folder (it does not touch any
       other Python on the PC)
    3. downloads the open-source packages AstroFlow uses (numpy, OpenCV, astropy, ...)
    4. checks that everything starts, then makes Desktop and Start-menu shortcuts.

  Internet is needed only while installing. Afterwards AstroFlow works fully offline.
  Running it again is safe: it repairs/updates the installation and keeps your settings and pictures
  (those live in %APPDATA%\AstroFlow and Documents\AstroFlow, never in the program folder).

  Usually started by double-clicking "Install AstroFlow.bat". Options:
    -Destination <folder>   install somewhere else (e.g. D:\AstroFlow)
    -Portable               build a self-contained folder only (no shortcuts) - copy it to any 64-bit Windows PC
    -NoShortcuts            skip the shortcuts
    -WithGpu                also install NVIDIA GPU acceleration (about 1 GB extra)
#>
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA "Programs\AstroFlow"),
    [switch]$Portable,
    [switch]$NoShortcuts,
    [switch]$WithGpu,
    [string[]]$PythonVersions = @("3.13.9", "3.13.8", "3.13.7", "3.12.10")
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"          # makes downloads much faster in Windows PowerShell 5
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Source = Split-Path -Parent $PSScriptRoot        # the folder that contains "Install AstroFlow.bat"

function Say([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Fail([string]$msg) { Write-Host ""; Write-Host "PROBLEM: $msg" -ForegroundColor Red; exit 1 }

if (-not [Environment]::Is64BitOperatingSystem) { Fail "AstroFlow needs 64-bit Windows." }
if (-not (Test-Path (Join-Path $Source "astroflow\__init__.py"))) { Fail "Run this from the AstroFlow folder (the one with 'Install AstroFlow.bat')." }

$Destination = [IO.Path]::GetFullPath($Destination)
Say "Installing AstroFlow into $Destination"
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$drive = (Get-Item $Destination).PSDrive
if ($drive -and $drive.Free -lt 2GB) { Fail "Less than 2 GB free on drive $($drive.Name):. Free some space first." }

# --------------------------------------------------------------- 1. program files
if ($Source -ne $Destination) {
    Say "Copying program files"
    $items = @("astroflow", "web", "data", "tools", "windows", "tests", "run.py", "README.md", "THIRD_PARTY.md",
               "HARDWARE_CHECKLIST.md", "ARCHITECTURE.md", "LICENSE", "requirements.txt", "requirements-optional.txt",
               "requirements-dev.txt", "Install AstroFlow.bat")
    foreach ($item in $items) {
        $p = Join-Path $Source $item
        if (Test-Path $p) {
            $target = Join-Path $Destination $item
            if ((Test-Path $target) -and (Get-Item $p).PSIsContainer) { Remove-Item $target -Recurse -Force }
            Copy-Item $p -Destination $Destination -Recurse -Force
        }
    }
    Get-ChildItem $Destination -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------- 2. Python
$Py = Join-Path $Destination "python"
$PyExe = Join-Path $Py "python.exe"
if (-not (Test-Path $PyExe)) {
    $zipFile = Join-Path $env:TEMP "astroflow-python-embed.zip"
    $got = $null
    foreach ($v in $PythonVersions) {
        $url = "https://www.python.org/ftp/python/$v/python-$v-embed-amd64.zip"
        try {
            Say "Downloading Python $v from python.org"
            Invoke-WebRequest -Uri $url -OutFile $zipFile -UseBasicParsing
            $got = $v
            break
        } catch {
            Write-Host "   (Python $v is not available - trying the next version)"
        }
    }
    if (-not $got) { Fail "Could not download Python from python.org. Check the internet connection and try again." }
    New-Item -ItemType Directory -Force -Path $Py | Out-Null
    Expand-Archive -Path $zipFile -DestinationPath $Py -Force
    Remove-Item $zipFile -Force
    # The embeddable Python ignores installed packages unless 'import site' is enabled in its ._pth file.
    $pth = Get-ChildItem $Py -Filter "python*._pth" | Select-Object -First 1
    $stdlib = (Get-ChildItem $Py -Filter "python*.zip" | Select-Object -First 1).Name
    Set-Content -Path $pth.FullName -Encoding ASCII -Value @($stdlib, ".", "Lib\site-packages", "..", "import site")
    Say "Installing pip (the Python package installer)"
    $getPip = Join-Path $Py "get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing
    & $PyExe $getPip --no-warn-script-location --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Fail "pip could not be installed." }
    Remove-Item $getPip -Force
}

# --------------------------------------------------------------- 3. packages
Say "Installing the packages AstroFlow uses (a few minutes - about 400 MB)"
& $PyExe -m pip install --upgrade --only-binary=:all: --no-warn-script-location --disable-pip-version-check -r (Join-Path $Destination "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "Installing packages failed - see the messages above. Run the installer again to retry." }
if ($WithGpu) {
    Say "Installing GPU acceleration (NVIDIA CUDA 12, about 1 GB)"
    & $PyExe -m pip install --no-warn-script-location --disable-pip-version-check "cupy-cuda12x>=13"
    if ($LASTEXITCODE -ne 0) { Write-Host "GPU package could not be installed - AstroFlow will use the processor instead." -ForegroundColor Yellow }
}

# --------------------------------------------------------------- 4. self-test
Say "Checking that everything starts"
Push-Location $Destination
& $PyExe -c "import numpy, scipy, cv2, astropy, fastapi, uvicorn, PIL, tifffile, psutil; import astroflow.server.app as a; print('AstroFlow', a.SOFTWARE_ID, 'is ready')"
$selfTest = $LASTEXITCODE
Pop-Location
if ($selfTest -ne 0) { Fail "The self-test failed - see the messages above." }

# --------------------------------------------------------------- 5. launcher + shortcuts
Copy-Item (Join-Path $Destination "windows\AstroFlow.vbs") (Join-Path $Destination "AstroFlow.vbs") -Force
Copy-Item (Join-Path $Destination "windows\AstroFlow (with log window).bat") (Join-Path $Destination "AstroFlow (with log window).bat") -Force
$launcher = Join-Path $Destination "AstroFlow.vbs"
$icon = Join-Path $Destination "windows\astroflow.ico"

if (-not $Portable -and -not $NoShortcuts) {
    Say "Creating shortcuts"
    $shell = New-Object -ComObject WScript.Shell
    $places = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))
    foreach ($dir in $places) {
        $lnk = $shell.CreateShortcut((Join-Path $dir "AstroFlow.lnk"))
        $lnk.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
        $lnk.Arguments = "`"$launcher`""
        $lnk.WorkingDirectory = $Destination
        $lnk.IconLocation = "$icon,0"
        $lnk.Description = "AstroFlow - capture, stack, process"
        $lnk.Save()
    }
}

Write-Host ""
Write-Host "AstroFlow is installed." -ForegroundColor Green
if ($Portable) {
    Write-Host "Portable folder: $Destination  - copy the whole folder anywhere and double-click AstroFlow.vbs inside it."
} else {
    Write-Host "Start it with the AstroFlow icon on your Desktop (or in the Start menu)."
}
Write-Host "Your settings: $env:APPDATA\AstroFlow    Your pictures: Documents\AstroFlow"
exit 0
