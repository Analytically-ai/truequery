@echo off
rem `truequery` for the agent when Claude Code runs commands in PowerShell
rem or cmd on Windows (Git Bash runs the sh script beside this file). It runs
rem this plugin's own engine, editable, so it is always the plugin's code.
setlocal
set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\truequery\pycache"
where uv >nul 2>nul
if errorlevel 1 (
  echo truequery: the TrueQuery plugin runs its checker with uv, which isn't installed. Install it from https://docs.astral.sh/uv/ and try again. 1>&2
  exit /b 127
)
uv run --quiet --no-project --python ">=3.11" --with-editable "%~dp0..\engine" -- truequery %*
exit /b %ERRORLEVEL%
