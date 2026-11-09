@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Kadry - server

set PY=
where py >nul 2>nul && set PY=py
if "%PY%"=="" where python >nul 2>nul && set PY=python
if "%PY%"=="" (
  echo.
  echo Python не найден на этом компьютере.
  echo Установите его с сайта python.org/downloads
  echo ^(в установщике поставьте галочку "Add python.exe to PATH"^),
  echo затем снова откройте этот файл.
  echo.
  pause
  exit /b 1
)

echo Закрываю старую копию программы, если она ещё работает...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>nul

echo Проверяю компоненты программы...
%PY% -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
  echo.
  echo Не удалось установить компоненты. Проверьте интернет и повторите.
  pause
  exit /b 1
)

echo.
echo Запускаю кадровую программу.
echo Откройте в браузере: http://localhost:5000
echo Это окно закрывать нельзя, пока вы работаете с программой.
echo.
%PY% app.py
echo.
echo Программа остановилась.
pause
