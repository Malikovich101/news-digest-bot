@echo off
setlocal
set "CODEX_RUNTIME_PYTHON=C:\Users\jonho\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%CODEX_RUNTIME_PYTHON%" (
  "%CODEX_RUNTIME_PYTHON%" "%~dp0get_session.py"
) else (
  py "%~dp0get_session.py"
)

echo.
echo The session string is shown above. Copy it and keep it private.
pause
