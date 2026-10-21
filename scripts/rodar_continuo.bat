@echo off
title LEVERAGE INVEST - Vigia do Bot
cd /d "C:\Users\Renato\Desktop\projetos code ia\BOTTELEGRAN"

:LOOP
echo [%date% %time%] Iniciando bot...
"C:\Users\Renato\AppData\Local\Python\pythoncore-3.14-64\python.exe" -W ignore -u bot_backtest.py
echo [%date% %time%] Bot parou. Reiniciando em 5 segundos...
timeout /t 5 /nobreak >nul
goto LOOP
