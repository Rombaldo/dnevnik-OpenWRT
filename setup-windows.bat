@echo off
rem Поставить «Мой дневник» на роутер с компьютера с Windows: вопросы простыми словами, дальше всё само.
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (py -3 tools\setup_computer.py %*) || (python tools\setup_computer.py %*)
echo.
pause
