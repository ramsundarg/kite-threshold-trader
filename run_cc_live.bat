@echo off
cd /d %~dp0
.venv\Scripts\python cc_trader.py --live >> cc_trader.log 2>&1
