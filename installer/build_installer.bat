@echo off
REM ============================================================
REM   MimiBox — сборка готового установщика (одной командой)
REM   1) собирает MimiBox.exe (PyInstaller, Python 3.12)
REM   2) кладёт рядом ядро и geo-файлы
REM   3) ставит Inno Setup (если нет) и компилирует Setup.exe
REM   Результат: installer\Output\MimiBox-Setup.exe
REM ============================================================
setlocal EnableExtensions
cd /d "%~dp0.."
echo(
echo ==== MimiBox installer build ====
echo(

REM 1/5) Building MimiBox.exe with Python 3.12...
echo [1/5] Building MimiBox.exe with Python 3.12...
py -3.12 --version >nul 2>nul || ( echo [!] Python 3.12 not found. Run:  py install 3.12  & pause & exit /b 1 )
py -3.12 -m pip install --upgrade pip >nul
py -3.12 -m pip install -r requirements.txt pyinstaller

REM Inject real version from git
echo [1b] Injecting version from git tag...
for /f "usebackq delims=" %%V in (`git describe --tags --always`) do set "RAW_VER=%%V"
echo   Version: %RAW_VER%
REM api.py
powershell -NoProfile -Command "$v='%RAW_VER%'.TrimStart('v'); $c=Get-Content api.py; $c=$c -replace 'BUILD_VERSION = \".*?\"', ('BUILD_VERSION = \"'+$v+'\"'); Set-Content api.py $c"
REM MimiBox.iss
powershell -NoProfile -Command "$v='%RAW_VER%'.TrimStart('v'); $c=Get-Content installer\MimiBox.iss; $c=$c -replace '#define MyAppVersion\s+\".*?\"', ('#define MyAppVersion   \"'+$v+'\"'); Set-Content installer\MimiBox.iss $c"

py -3.12 -m PyInstaller --noconfirm --onedir --windowed --noupx --name MimiBox --icon "ui/app.ico" --collect-all webview --collect-all pystray --add-data "ui;ui" main.py
if not exist "dist\MimiBox\MimiBox.exe" ( echo [!] Build failed. Restoring... & git checkout -- api.py installer/MimiBox.iss & pause & exit /b 1 )

REM 2/5) Copy updated index.html and font
echo [2/5] Copying updated index.html and font...
if exist "dist\MimiBox\index.html" (
    echo   index.html exists (PyInstaller may have copied it)
) else (
    echo   index.html not found, attempting to copy from ui\\index.html
    copy /y "ui\index.html" "dist\MimiBox\" >nul
)
copy /y "ui\font.ttf" "dist\MimiBox\" >nul
copy /y "ui\font.ttf" "dist\MimiBox\ui\" >nul

REM 3/5) Copy core runtime files
REM xray.exe + tun2socks.exe + wintun.dll needed for "Tunnel" mode
echo [3/5] Copying runtime files...
for %%F in (xray.exe tun2socks.exe geoip.dat geosite.dat wintun.dll) do (
    if exist "%%F" ( copy /y "%%F" "dist\MimiBox\" >nul ) else ( echo   [warn] missing %%F )
)

REM 4/5) Locate Inno Setup compiler (ISCC)...
echo [4/5] Locating Inno Setup compiler (ISCC)...
set "ISCC="
for %%P in (iscc.exe) do if not defined ISCC set "ISCC=%%~$PATH:P"
REM %LOCALAPPDATA% тоже проверяем: winget ставит Inno Setup для пользователя,
REM и тогда в Program Files его нет.
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo   Inno Setup not found - installing via winget...
    winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements --silent
    if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
    if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
)
if not defined ISCC (
    echo(
    echo [!] Inno Setup is unavailable. Use the no-dependency installer instead:
    echo     installer\MimiBox-portable-setup.bat
    pause & exit /b 1
)

REM 5/5) Compile installer
echo [5/5] Compiling installer...
"%ISCC%" "installer\MimiBox.iss"
if exist "installer\Output\MimiBox-Setup.exe" (
    echo(
    echo  SUCCESS ^-^> installer\Output\MimiBox-Setup.exe
) else (
    echo [!] Compilation failed - check messages above.
)
echo Restoring api.py and MimiBox.iss...
git checkout -- api.py installer/MimiBox.iss
echo(
pause
