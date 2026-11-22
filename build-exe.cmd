@echo off
rem Build decode-split.exe for Windows (one file, no console window) into dist\.
rem Needs Python 3 with Tk (the python.org installer has it) and PyInstaller:
rem   python -m pip install pyinstaller
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --windowed --name decode-split ^
  --distpath "%~dp0dist" --workpath "%TEMP%\decode-split-build" --specpath "%TEMP%\decode-split-build" ^
  decode-split.py
