@echo off
cd /d "%~dp0"
echo This will delete: .kis_token.json, __pycache__, build, dist, DantaScanner.spec, DantaScanner.exe (old engine)
echo Your keys (kis_config.json), settings and Excel records are NOT touched.
pause
if exist ".kis_token.json" del /f /q ".kis_token.json"
if exist "__pycache__" rmdir /s /q "__pycache__"
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "DantaScanner.spec" del /f /q "DantaScanner.spec"
if exist "DantaScanner.exe" del /f /q "DantaScanner.exe"
echo DONE
pause
