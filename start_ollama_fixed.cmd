@echo off
setlocal
rem Start the local Ollama API server before opening its tray application.
rem This avoids inheriting a stale model path from Explorer and ensures that
rem port 11434 is actually listening after Windows sign-in.
set "ROOT=%~dp0"
set "OLLAMA_HOME=%LOCALAPPDATA%\Programs\Ollama"
set "OLLAMA_MODELS=%ROOT%_dependencies\ollama_user_data_live\models"
set "OLLAMA_HOST=127.0.0.1:11434"
set "OLLAMA_NO_CLOUD=1"

if not exist "%OLLAMA_HOME%\ollama.exe" exit /b 1
if not exist "%OLLAMA_MODELS%\manifests-v2\ollama.com\library" exit /b 2

rem Do not start a second server if Ollama is already listening.
curl.exe --silent --fail --max-time 2 http://127.0.0.1:11434/api/version >nul 2>&1
if errorlevel 1 start "" /B /D "%OLLAMA_HOME%" "%OLLAMA_HOME%\ollama.exe" serve

start "" /D "%OLLAMA_HOME%" "%OLLAMA_HOME%\ollama app.exe" --hide --fast-startup
