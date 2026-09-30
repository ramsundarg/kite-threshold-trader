@echo off
cd /d %~dp0
.venv\Scripts\python trader.py --live >> trader.log 2>&1
