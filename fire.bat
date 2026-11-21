@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Project .venv is missing. Install project requirements first.
  exit /b 1
)
if "%~2"=="" (
  echo Usage: fire node-1 60
  echo Duration is real seconds. Keep this window open until completion.
  exit /b 1
)
set "DEMO_SPEEDUP=60"
".venv\Scripts\python.exe" -m gateway.sim.timed_demo "%~1" --seconds "%~2"
exit /b %ERRORLEVEL%
