@echo off
rem Builds dist\PeripheralsBattery\PeripheralsBattery.exe (a folder build: it starts
rem faster than a one-file .exe, which would unpack itself to %TEMP% on every boot).
python -m pip install --upgrade -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name PeripheralsBattery --icon docs\app.ico ^
  --add-data "peribatt\recipes.json;peribatt" ^
  --hidden-import pystray._win32 ^
  --exclude-module unittest --exclude-module pydoc ^
  --exclude-module test --exclude-module lib2to3 --exclude-module xmlrpc ^
  --exclude-module pystray._xorg --exclude-module pystray._gtk ^
  --exclude-module pystray._appindicator --exclude-module pystray._darwin ^
  peribatt.pyw || exit /b 1
echo Built dist\PeripheralsBattery\PeripheralsBattery.exe
