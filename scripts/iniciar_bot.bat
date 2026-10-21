@echo off
chcp 65001 >nul
echo ============================================
echo  LEVERAGE INVEST - Bot Telegram
echo ============================================
cd /d "%~dp0.."
echo Pasta: %CD%
"C:\Users\Renato\AppData\Local\Python\pythoncore-3.14-64\python.exe" -W ignore bot_backtest.py
pause
