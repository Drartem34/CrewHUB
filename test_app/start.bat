@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python не знайдено. Встановіть Python 3.10+ з https://www.python.org/downloads/
  echo Під час встановлення поставте галочку "Add python.exe to PATH".
  pause
  exit /b 1
)
rem Бібліотеки ставимо лише якщо їх ще немає: звичайний запуск не потребує інтернету
python -c "import flask, waitress" >nul 2>nul || python -m pip install -q -r requirements.txt
rem Якщо сервер впаде, він сам підніметься через 5 секунд
:loop
python start.py
if %errorlevel%==0 goto end
echo Сервер зупинився з помилкою, перезапуск через 5 секунд... (закрийте вікно, щоб вийти)
timeout /t 5 /nobreak >nul
goto loop
:end
pause
