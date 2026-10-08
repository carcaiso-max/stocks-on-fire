@echo off
title Stocks on fire (servidor - no cerrar mientras uses la app)
cd /d "%~dp0"
python app_13f.py || pause
