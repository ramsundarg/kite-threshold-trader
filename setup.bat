@echo off
cd /d %~dp0
py -3 -m venv .venv || goto :err
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt || goto :err
if not exist config.json copy config.example.json config.json
.venv\Scripts\python -m unittest test_strategy || goto :err
echo.
echo Setup OK. Edit config.json, then run run_demo.bat
pause
exit /b 0
:err
echo Setup FAILED - see messages above.
pause
exit /b 1
