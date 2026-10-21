@echo off
rem Double-click this to run AstroFlow. The first time it installs AstroFlow (internet needed once).
set "APP=%LOCALAPPDATA%\Programs\AstroFlow"
if not exist "%APP%\python\pythonw.exe" (
  echo AstroFlow is not installed yet - installing it now. This takes a few minutes, the first time only.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install_windows.ps1"
  if errorlevel 1 (
    echo.
    echo Installation did not finish - see the message above.
    pause
    exit /b 1
  )
)
start "" wscript.exe "%APP%\AstroFlow.vbs"
