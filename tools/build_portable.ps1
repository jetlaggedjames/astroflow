<#
  Builds the portable AstroFlow folder (and a .zip of it) on a Windows PC with internet:
      powershell -ExecutionPolicy Bypass -File tools\build_portable.ps1
  Result: dist\AstroFlow-Portable\  (copy anywhere, double-click AstroFlow.vbs)  and  dist\AstroFlow-Portable.zip
  To make a classic Setup.exe from it, open windows\AstroFlow.iss in Inno Setup (free) and press Compile.
#>
param([string]$Out = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist\AstroFlow-Portable"), [switch]$WithGpu)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
$psArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $root "windows\install_windows.ps1"), "-Destination", $Out, "-Portable")
if ($WithGpu) { $psArgs += "-WithGpu" }
& powershell @psArgs
if ($LASTEXITCODE -ne 0) { throw "Building the portable folder failed" }
# Byte-code caches are rebuilt on first start; leave them out of the archive.
Get-ChildItem $Out -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
$zip = "$Out.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $Out -DestinationPath $zip
Write-Host "Portable folder: $Out"
Write-Host "Zip archive:     $zip"
