@echo off
REM ============================================================
REM   MimiBox — установщик без сторонних программ
REM   Ставит приложение для текущего пользователя (без прав админа),
REM   создаёт ярлыки на рабочем столе и в меню Пуск.
REM   Файлы (LDK2ray.exe, xray.exe, geoip.dat, geosite.dat, wintun.dll)
REM   должны лежать РЯДОМ с этим файлом или в ..\dist
REM ============================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Установка MimiBox

REM --- где лежит полезная нагрузка ---
set "SRC="
if exist "%~dp0LDK2ray.exe" set "SRC=%~dp0"
if not defined SRC if exist "%~dp0..\dist\LDK2ray\LDK2ray.exe" set "SRC=%~dp0..\dist\LDK2ray\"
if not defined SRC if exist "%~dp0..\dist\LDK2ray.exe" set "SRC=%~dp0..\dist\"
if not defined SRC (
    echo [!] Не найден LDK2ray.exe рядом с установщиком.
    echo     Положите файлы приложения рядом с этим .bat и запустите снова.
    pause & exit /b 1
)

set "DEST=%LOCALAPPDATA%\Programs\MimiBox"
echo(
echo   Установка MimiBox в:
echo   %DEST%
echo(

mkdir "%DEST%" 2>nul
for %%F in (LDK2ray.exe xray.exe tun2socks.exe geoip.dat geosite.dat wintun.dll) do (
    if exist "%SRC%%%F" (
        copy /y "%SRC%%%F" "%DEST%\" >nul && echo   [ok] %%F
    ) else (
        echo   [warn] нет файла %%F
    )
)
REM onedir-сборка тащит за собой папку _internal — без неё exe не стартует
if exist "%SRC%_internal" (
    echo   [..] копирую _internal
    xcopy /e /i /y /q "%SRC%_internal" "%DEST%\_internal" >nul && echo   [ok] _internal
)

echo(
echo   Создаю ярлыки...
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -Command"
%PS% "$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'MimiBox.lnk')); $s.TargetPath='%DEST%\LDK2ray.exe'; $s.WorkingDirectory='%DEST%'; $s.Save()"
%PS% "$m=[IO.Path]::Combine([Environment]::GetFolderPath('Programs'),'MimiBox'); New-Item -ItemType Directory -Force -Path $m ^| Out-Null; $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut([IO.Path]::Combine($m,'MimiBox.lnk')); $s.TargetPath='%DEST%\LDK2ray.exe'; $s.WorkingDirectory='%DEST%'; $s.Save()"

echo(
echo   Готово! Ярлык «MimiBox» на рабочем столе и в меню Пуск.
echo(
choice /c YN /n /m "Запустить MimiBox сейчас? [Y/N] "
if errorlevel 2 goto :end
start "" "%DEST%\LDK2ray.exe"
:end
endlocal
