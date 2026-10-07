@echo off
rem VocalChain - crea el entorno la primera vez, instala dependencias y abre la app
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Creando entorno virtual...
    py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist .venv\Scripts\python.exe (
        echo No encontre Python. Instala Python 3.12 desde python.org ^(marca "Add to PATH"^).
        pause
        exit /b 1
    )
    .venv\Scripts\python -m pip install --upgrade pip
)
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python -m vocalchain
if errorlevel 1 pause
