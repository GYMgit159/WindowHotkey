@echo off
rem Build WindowHotkey.exe (one file, no console window).
rem Requires: Python 3.10+ on PATH. Output goes to dist\WindowHotkey.exe
cd /d "%~dp0"

set "ICONARG="
if exist icon.ico set "ICONARG=--icon icon.ico --add-data icon.ico;."

python -m pip install -r requirements.txt || goto :fail

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name WindowHotkey %ICONARG% ^
  window_hotkey_qt.py || goto :fail

echo.
if "%ICONARG%"=="" echo note: icon.ico not found, using the default Windows icon.
echo OK  --  dist\WindowHotkey.exe
pause
exit /b 0

:fail
echo.
echo BUILD FAILED, see the messages above.
pause
exit /b 1
