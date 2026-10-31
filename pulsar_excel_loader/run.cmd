@echo off
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0pulsar_loader.py" %*
  exit /b
)
py -3 "%~dp0pulsar_loader.py" %*
