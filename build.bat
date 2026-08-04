@echo off
echo ============================================
echo   Building MimiBox.exe
echo ============================================
echo.

REM --- Python 3.12 is required (pywebview/pythonnet do not support 3.14 yet) ---
py -3.12 --version >nul 2>nul
if errorlevel 1 (
    echo [!] Python 3.12 is not installed.
    echo     Run this command once, then start build.bat again:
    echo.
    echo         py install 3.12
    echo.
    pause
    exit /b 1
)

echo [1/4] Installing dependencies with Python 3.12...
py -3.12 -m pip install --upgrade pip
py -3.12 -m pip install -r requirements.txt pyinstaller

echo.
echo.
echo [2/4] Injecting real version from git tag...
for /f "usebackq delims=" %%V in (`git describe --tags --always`) do set "RAW_VER=%%V"
powershell -NoProfile -Command "$v='%RAW_VER%'.TrimStart('v'); $c=Get-Content api.py; $c=$c -replace 'BUILD_VERSION = \".*?\"', ('BUILD_VERSION = \"'+$v+'\"'); Set-Content api.py $c"
echo   Version: %RAW_VER%

echo.
echo [3/4] Building app (onedir - легче по памяти и быстрее старт)...
REM --noupx: сжатие UPX ломает библиотеки WebView2 и вызывает подозрения антивирусов
py -3.12 -m PyInstaller --noconfirm --onedir --windowed --noupx --name MimiBox --icon "ui/app.ico" --collect-all webview --collect-all pystray --add-data "ui;ui" main.py

echo.
echo [4/4] Restoring api.py...
git checkout -- api.py

REM -- Add authentication resources needed for subscription system
py -3.12 -c "import os, shutil; dst = 'dist\\MimiBox'; shutil.copy2('ui\\index.html', dst + '\\index.html')" 2>nul

echo.
echo [extra] Copying core + geo files next to the app...
REM tun2socks.exe + wintun.dll нужны для режима "Туннель"
for %%F in (xray.exe tun2socks.exe geoip.dat geosite.dat wintun.dll) do (
    if exist "%%F" copy /y "%%F" "dist\MimiBox\" >nul
)

if exist dist\MimiBox\MimiBox.exe (
    echo  SUCCESS: dist\MimiBox\  is ready ^(run MimiBox.exe inside^).
    if not exist dist\MimiBox\xray.exe echo  Reminder: put xray.exe into dist\MimiBox\ next to MimiBox.exe.
    if not exist dist\MimiBox\tun2socks.exe echo  Reminder: tun2socks.exe is missing - Tunnel mode will be unavailable.
) else (
    echo  [!] Build failed - check the messages above.
)
echo.
pause
