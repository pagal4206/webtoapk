@echo off
setlocal

set "PROJECT_DIR=%~dp0"

if "%REMOTE_BUILDER_BASE_URL%"=="" if not "%~1"=="" set "REMOTE_BUILDER_BASE_URL=%~1"
if "%REMOTE_BUILDER_BASE_URL%"=="" (
  echo Set REMOTE_BUILDER_BASE_URL before running this script or pass it as the first argument.
  exit /b 1
)

if "%PORT%"=="" set "PORT=8090"

cd /d "%PROJECT_DIR%"
python -m pip install -r requirements.txt || exit /b 1
python app.py
