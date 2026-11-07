@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

echo Installing PyInstaller...
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 goto fail

echo Building GalleryPckPatcher.exe...
%PY% -m PyInstaller --noconfirm --onefile --windowed --name GalleryPckPatcher gallery_pck_gui.py
if errorlevel 1 goto fail

echo.
echo Build OK: %~dp0dist\GalleryPckPatcher.exe
goto end

:fail
echo.
echo Build FAILED. Check that Python 3 is installed and added to PATH.

:end
pause
