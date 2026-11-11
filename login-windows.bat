@echo off
rem Войти в Дневник.ру через Госуслуги на компьютере с Windows и передать вход на роутер.
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (py -3 tools\login_computer.py %*) || (python tools\login_computer.py %*)
echo.
pause
