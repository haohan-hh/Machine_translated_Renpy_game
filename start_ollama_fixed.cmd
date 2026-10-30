@echo off
setlocal
rem Let the tray app own and supervise its API server. The previous serve-first
rem order made the app's own child fail to bind and retry once per second.
set "ROOT=%~dp0"
set "OLLAMA_HOME=%LOCALAPPDATA%\Programs\Ollama"
set "OLLAMA_MODELS=%ROOT%_dependencies\ollama_user_data_live\models"
set "OLLAMA_HOST=127.0.0.1:11434"
set "OLLAMA_NO_CLOUD=1"

if not exist "%OLLAMA_HOME%\ollama.exe" exit /b 1
if not exist "%OLLAMA_HOME%\ollama app.exe" exit /b 4
if not exist "%OLLAMA_MODELS%\manifests-v2\ollama.com\library" exit /b 2

rem Do not launch a second tray instance or server when the API is ready.
curl.exe --silent --fail --max-time 2 http://127.0.0.1:11434/api/version >nul 2>&1
if not errorlevel 1 exit /b 0

rem Ollama's Windows app starts and supervises ollama.exe serve itself.
start "" /D "%OLLAMA_HOME%" "%OLLAMA_HOME%\ollama app.exe" --hide --fast-startup
set /a WAITED=0
:wait_for_server
curl.exe --silent --fail --max-time 2 http://127.0.0.1:11434/api/version >nul 2>&1
if not errorlevel 1 exit /b 0
set /a WAITED+=1
if %WAITED% GEQ 60 goto server_failed
timeout /t 1 /nobreak >nul
goto wait_for_server

:server_failed
echo Ollama app did not make the API ready at 127.0.0.1:11434 within 60 seconds.
echo Check %LOCALAPPDATA%\Ollama\app.log for startup details.
pause
exit /b 3
