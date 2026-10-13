@echo off
title Stocks on fire (servidor - no cerrar mientras uses la app)
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  No se encuentra Python en este ordenador.
    echo.
    echo  1. Descargalo de https://www.python.org/downloads/ ^(se abre ahora en el navegador^).
    echo  2. En la primera pantalla del instalador MARCA la casilla "Add python.exe to PATH".
    echo  3. Cuando termine, vuelve a hacer doble clic en iniciar.bat.
    echo.
    start "" https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import pandas, numpy" >nul 2>&1
if errorlevel 1 (
    echo Instalando las librerias necesarias ^(solo la primera vez, tarda un minuto^)...
    python -m pip install --user -r requirements.txt
    if errorlevel 1 (
        echo.
        echo  No se pudieron instalar las librerias. Revisa tu conexion a internet y vuelve a intentarlo.
        pause
        exit /b 1
    )
)

python app_13f.py || pause
