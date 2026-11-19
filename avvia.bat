@echo off
setlocal
cd /d "%~dp0"
title Inventario Negozio
chcp 65001 >nul

rem ---- Non serve "activate": si usa direttamente il Python dentro .venv ----
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python non trovato. Installalo da https://www.python.org/downloads/
  echo  spuntando la casella "Add python.exe to PATH", poi riapri questo file.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo  Prima installazione: preparo l'ambiente, attendi qualche minuto...
  python -m venv .venv
  if errorlevel 1 goto errore
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, multipart, PIL, requests, google.genai, qrcode" >nul 2>nul
if errorlevel 1 (
  echo  Installo i componenti necessari...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 goto errore
)

".venv\Scripts\python.exe" server.py
pause
exit /b 0

:errore
echo.
echo  Errore durante l'installazione. Controlla la connessione a Internet e riprova.
pause
exit /b 1
