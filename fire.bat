@echo off
rem 60-second demo fire:   fire node-3        (default 60 s)
rem Other length:          fire node-3 90
rem Smoke is injected, the alarm appears after about 15 s, and the alarm is
rem cleared at the end. Keep this window open until "Timed demo complete".
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "C:\venvs\vip\Scripts\python.exe" set "PY=C:\venvs\vip\Scripts\python.exe"
if not defined PY if defined VIRTUAL_ENV set "PY=%VIRTUAL_ENV%\Scripts\python.exe"
if not defined PY (
  echo ERROR: no Python environment found ^(.venv, C:\venvs\vip or an activated venv^).
  exit /b 1
)
if "%~1"=="" (
  echo Usage: fire node-3 [seconds]   ^(default 60^)
  echo Duration is real seconds. Keep this window open until completion.
  exit /b 1
)
set "SECONDS=%~2"
if "%SECONDS%"=="" set "SECONDS=60"
set "DEMO_SPEEDUP=60"
"%PY%" -m gateway.sim.timed_demo "%~1" --seconds "%SECONDS%"
exit /b %ERRORLEVEL%
