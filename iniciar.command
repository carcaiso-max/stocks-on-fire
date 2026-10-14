#!/bin/bash
# Arranque para Mac (doble clic en Finder) y Linux. Instala lo necesario en .venv la primera vez.
cd "$(dirname "$0")" || exit 1

fallo() {
    echo
    echo "  $1"
    echo
    read -r -p "Pulsa Enter para cerrar esta ventana..." _
    exit 1
}

PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PY="$c"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "No se encuentra Python 3.10 o superior."
    echo "Descárgalo de https://www.python.org/downloads/ (se abre ahora), instálalo y vuelve a abrir este fichero."
    if command -v open >/dev/null 2>&1; then open "https://www.python.org/downloads/"; fi
    fallo "Falta Python."
fi

if [ ! -x .venv/bin/python ]; then
    echo "Preparando la app (solo la primera vez, tarda un par de minutos)..."
    "$PY" -m venv .venv || fallo "No se pudo crear el entorno de Python. En Linux instala el paquete python3-venv."
fi

if ! .venv/bin/python -c "import pandas, numpy" >/dev/null 2>&1; then
    echo "Instalando las librerías necesarias..."
    .venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1
    .venv/bin/python -m pip install -r requirements.txt || fallo "No se pudieron instalar las librerías. Revisa tu conexión a internet."
fi

echo "Stocks on fire: no cierres esta ventana mientras uses la app."
exec .venv/bin/python app_13f.py
