@echo off
setlocal
cd /d "%~dp0\.."
python scripts\build.py
if errorlevel 1 exit /b %errorlevel%
echo Build finished. See dist\VRCJoyCon
endlocal