@echo off
cd /d "%~dp0"
python -m pip install pyinstaller requests openpyxl
python -m PyInstaller --noconfirm --onefile --windowed --name DantaScanner --hidden-import danta_v16 DantaScanner_GUI.py
copy /Y dist\DantaScanner.exe DantaScanner.exe
echo.
echo DONE: DantaScanner.exe
pause
