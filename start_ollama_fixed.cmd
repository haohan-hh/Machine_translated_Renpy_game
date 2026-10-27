@echo off
rem Start Ollama with an explicit persistent model repository.
rem This avoids inheriting a stale OLLAMA_MODELS value from Explorer.
set "OLLAMA_MODELS=E:\翻译\_dependencies\ollama_user_data_live\models"
set "OLLAMA_HOST=127.0.0.1:11434"
set "OLLAMA_NO_CLOUD=1"
start "" /D "C:\Users\HaoHan\AppData\Local\Programs\Ollama" "C:\Users\HaoHan\AppData\Local\Programs\Ollama\ollama app.exe" --hide --fast-startup
