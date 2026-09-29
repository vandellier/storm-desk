@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo StormDesk needs the local environment in .venv
  echo Create it with: uv venv --python 3.11 .venv ^&^& uv pip install -r requirements.txt --python .venv\Scripts\python.exe
  exit /b 1
)
".venv\Scripts\python.exe" main.py
