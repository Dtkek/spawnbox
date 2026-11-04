@echo off
chcp 65001 >nul
title Spawnbox
cd /d "%~dp0"

rem Запуск из исходников. Обычным игрокам нужен готовый spawnbox.exe, Python для него не нужен
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 app.py
    goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python app.py
    goto done
)
echo Не найден Python. Скачай готовый spawnbox.exe, для него Python не нужен:
echo     https://github.com/Dtkek/spawnbox/releases/latest
echo или установи Python 3.11 и новее: https://www.python.org/downloads/

:done
pause
