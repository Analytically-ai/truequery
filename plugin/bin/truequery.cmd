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
rem `truequery setup data` says which installer to use by this variable.
set "TRUEQUERY_LAUNCHER=plugin"
rem After `truequery setup data --yes`, the data reader (DuckDB) joins the
rem environment. Hooks don't run this launcher, so they never load it.
set "TQCONF=%XDG_CONFIG_HOME%"
if "%TQCONF%"=="" set "TQCONF=%USERPROFILE%\.config"
rem (goto, not a parenthesized block: inside one, %ERRORLEVEL% would be
rem read before uv runs)
if exist "%TQCONF%\truequery\data-reader" goto withdata
uv run --quiet --no-project --python ">=3.11" --with-editable "%~dp0..\engine" -- truequery %*
exit /b %ERRORLEVEL%
:withdata
uv run --quiet --no-project --python ">=3.11" --with-editable "%~dp0..\engine" --with "duckdb>=1.2.0" -- truequery %*
exit /b %ERRORLEVEL%
