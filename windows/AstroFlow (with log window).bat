@echo off
rem Starts AstroFlow with a console window that shows messages - useful when something does not work.
cd /d "%~dp0"
if exist "%~dp0python\python.exe" (set PY="%~dp0python\python.exe") else (set PY="%~dp0..\python\python.exe" & cd /d "%~dp0..")
%PY% run.py --verbose %*
pause
