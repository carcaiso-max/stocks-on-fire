#!/bin/sh
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
    echo "No se encuentra Python 3. Instálalo desde https://www.python.org/downloads/ y vuelve a ejecutar este fichero."
    exit 1
fi
if ! python3 -c "import pandas, numpy" >/dev/null 2>&1; then
    echo "Instalando las librerías necesarias (solo la primera vez)..."
    python3 -m pip install --user -r requirements.txt || { echo "No se pudieron instalar las librerías."; exit 1; }
fi
exec python3 app_13f.py
