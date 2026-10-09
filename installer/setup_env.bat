@echo off
rem VocalChain - prepara el entorno de Python (.venv) e instala/actualiza las dependencias.
rem Lo ejecuta el instalador y, si hace falta, el lanzador VocalChain.exe.
title VocalChain - preparando el entorno
cd /d "%~dp0"
echo.
echo  VocalChain: preparando el entorno de Python...
echo  (la primera vez tarda unos minutos; las siguientes es casi instantaneo)
echo.
if not exist .venv\Scripts\python.exe (
    py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist .venv\Scripts\python.exe (
        echo.
        echo  No encontre Python. Instala Python 3.12 desde python.org ^(marca "Add to PATH"^)
        echo  y vuelve a abrir VocalChain.
        pause
        exit /b 1
    )
    .venv\Scripts\python -m pip install --upgrade pip --disable-pip-version-check -q
)
.venv\Scripts\python -m pip install -r requirements.txt --disable-pip-version-check -q
if errorlevel 1 (
    echo.
    echo  Hubo un problema instalando dependencias ^(revisa tu conexion a internet^).
    pause
    exit /b 1
)
echo.
echo  Listo.
exit /b 0
