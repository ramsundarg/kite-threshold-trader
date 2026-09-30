@echo off
cd /d %~dp0
.venv\Scripts\python cc_trader.py --demo
pause
