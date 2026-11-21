@echo off
setlocal
set "RPY_OLLAMA_API=http://127.0.0.1:11434/api/version"
rem A running service owns its model store, even if this copy has no bundle.
curl.exe --noproxy "*" --silent --fail --max-time 2 "%RPY_OLLAMA_API%" >nul 2>&1
if not errorlevel 1 exit /b 0

rem A manually opened tray app starts and supervises the API itself.
tasklist.exe /FI "IMAGENAME eq ollama app.exe" /FO CSV /NH 2>nul | findstr.exe /I /L /C:"ollama app.exe" >nul
if not errorlevel 1 goto wait_for_server

set "RPY_OLLAMA_DIR=%LOCALAPPDATA%\Programs\Ollama"
if not exist "%RPY_OLLAMA_DIR%\ollama app.exe" set "RPY_OLLAMA_DIR=%~dp0_dependencies\ollama_program"
if not exist "%RPY_OLLAMA_DIR%\ollama app.exe" exit /b 4
if not defined OLLAMA_MODELS if exist "%~dp0_dependencies\ollama_user_data_live\models\" set "OLLAMA_MODELS=%~dp0_dependencies\ollama_user_data_live\models"
set "OLLAMA_HOST=127.0.0.1:11434"
if not defined OLLAMA_NO_CLOUD set "OLLAMA_NO_CLOUD=1"
start "" /B /D "%RPY_OLLAMA_DIR%" "%RPY_OLLAMA_DIR%\ollama app.exe" --hide --fast-startup
if errorlevel 1 exit /b 1

:wait_for_server
rem This also runs with stdin connected to NUL when started by the translator.
rem Use a wall-clock deadline and a timer independent of any console input.
powershell.exe -NoLogo -NoProfile -NonInteractive -Command ^
  "$timer = [Diagnostics.Stopwatch]::StartNew();" ^
  "do {" ^
  "  try { if ((Invoke-RestMethod -Uri $env:RPY_OLLAMA_API -TimeoutSec 2 -ErrorAction Stop).version) { exit 0 } } catch {};" ^
  "  Start-Sleep -Milliseconds 500;" ^
  "} while ($timer.Elapsed.TotalSeconds -lt 60); exit 3"
if not errorlevel 1 exit /b 0
echo Ollama did not make its local API ready within 60 seconds. Check the Ollama app log. 1>&2
exit /b 3
