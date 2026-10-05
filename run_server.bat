@echo off
rem Gauge 웹앱 실행: 더블클릭하면 서버가 켜지고, 브라우저에서 http://127.0.0.1:8765 를 열면 됩니다.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Gauge 서버를 시작합니다...  종료하려면 이 창에서 Ctrl+C
echo.
python server.py
echo.
echo 서버가 종료되었습니다.
pause
