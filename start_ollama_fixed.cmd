@echo off
rem Start Ollama with an explicit persistent model repository.
rem This avoids inheriting a stale OLLAMA_MODELS value from Explorer.
set "ROOT=%~dp0"
set "OLLAMA_MODELS=%ROOT%_dependencies\ollama_user_data_live\models"
set "OLLAMA_HOST=127.0.0.1:11434"
set "OLLAMA_NO_CLOUD=1"
start "" /D "%LOCALAPPDATA%\Programs\Ollama" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" --hide --fast-startup
