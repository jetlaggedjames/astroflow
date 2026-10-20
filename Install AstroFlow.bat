@echo off
title Installing AstroFlow
echo.
echo  Installing AstroFlow. Internet is needed once (about 400 MB of downloads).
echo  Nothing needs administrator rights. This window closes by itself when done.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install_windows.ps1" %*
if errorlevel 1 (
  echo.
  echo  Something went wrong - the message above says what. Fix it and run this again;
  echo  it continues where it stopped.
)
echo.
pause
