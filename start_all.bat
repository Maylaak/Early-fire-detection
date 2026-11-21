@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto folder_error
if not exist ".venv\Scripts\activate.bat" goto venv_error
call ".venv\Scripts\activate.bat"
if errorlevel 1 goto venv_error
set "DEMO_SPEEDUP=60"
set "WILDFIRE_LOCAL_DEMO=1"
if not exist ".venv\Scripts\python.exe" goto venv_error
".venv\Scripts\python.exe" --version
if errorlevel 1 goto venv_error
if /i "%~1"=="--worker" goto worker

rem Prevent MQTT session takeover by a second detection service/simulator.
powershell -NoProfile -NonInteractive -Command "$p=Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match '-m gateway\.(service|web|sim)(\s|$)' }; if($p){exit 1}"
if errorlevel 1 (
    echo ERROR: Gateway processes are already running, or the process check failed.
    echo Stop existing service, web and simulator processes before running this launcher.
    goto failed
)

title Wildfire - Docker and launcher
if not exist "gateway\" goto project_error
set "COMPOSE_FOUND="
for %%F in (compose.yaml compose.yml docker-compose.yaml docker-compose.yml) do if exist "%%F" set "COMPOSE_FOUND=1"
if not defined COMPOSE_FOUND goto project_error

rem Support Docker Desktop even if its CLI was not added to PATH yet.
set "PATH=%PATH%;%ProgramFiles%\Docker\Docker\resources\bin;%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin"
where docker >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker CLI not found. Install Docker Desktop, then retry.
    goto failed
)
docker compose version
if errorlevel 1 (
    echo ERROR: Docker Compose is unavailable. Repair or update Docker Desktop.
    goto failed
)
docker info >nul 2>&1
if not errorlevel 1 goto docker_ready
set "DOCKER_DESKTOP="
if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" set "DOCKER_DESKTOP=%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
if not defined DOCKER_DESKTOP if exist "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe" set "DOCKER_DESKTOP=%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"
if not defined DOCKER_DESKTOP (
    echo ERROR: Cannot locate Docker Desktop. Start it manually and retry.
    goto failed
)
echo Starting Docker Desktop...
start "" "%DOCKER_DESKTOP%"
if errorlevel 1 goto failed
set /a ATTEMPTS=0
:wait_docker
docker info >nul 2>&1
if not errorlevel 1 goto docker_ready
set /a ATTEMPTS+=1
if %ATTEMPTS% GEQ 60 (
    echo ERROR: Docker engine did not become ready. Check Docker Desktop and WSL.
    goto failed
)
timeout /t 3 /nobreak >nul
goto wait_docker

:docker_ready
echo Starting PostgreSQL and Mosquitto...
docker compose up -d
if errorlevel 1 (
    echo ERROR: Docker Compose failed. No gateway processes were launched.
    goto failed
)
rem The README names the database service postgres and its user gateway.
echo Waiting for PostgreSQL...
set /a ATTEMPTS=0
:wait_postgres
docker compose exec -T postgres pg_isready -U gateway -d wildfire >nul 2>&1
if not errorlevel 1 goto launch
set /a ATTEMPTS+=1
if %ATTEMPTS% GEQ 40 (
    echo ERROR: PostgreSQL did not become ready. No gateway processes were launched.
    docker compose ps
    goto failed
)
timeout /t 3 /nobreak >nul
goto wait_postgres

:launch
echo Docker is ready. Waiting 5 seconds before starting detection...
timeout /t 5 /nobreak >nul
start "Wildfire - Detection service" "%ComSpec%" /d /k ""%~f0" --worker service"
if errorlevel 1 goto launch_error
echo Waiting 5 seconds before starting the simulator...
timeout /t 5 /nobreak >nul
start "Wildfire - Simulator" "%ComSpec%" /d /k ""%~f0" --worker simulator"
if errorlevel 1 goto launch_error
echo Waiting 5 seconds before starting the web console...
timeout /t 5 /nobreak >nul
start "Wildfire - Web console" "%ComSpec%" /d /k ""%~f0" --worker web"
if errorlevel 1 goto launch_error
echo.
echo Three process windows opened. Check their output for startup errors.
echo Dashboard: http://127.0.0.1:8000/
echo Warm-up takes approximately 5 minutes at DEMO_SPEEDUP=60.
echo Stop Python processes with Ctrl+C in their windows.
echo Containers remain running; use docker compose down to stop them.
echo Close existing process windows before running this launcher again.
pause
exit /b 0

:worker
if /i "%~2"=="service" goto service
if /i "%~2"=="simulator" goto simulator
if /i "%~2"=="web" goto web
echo ERROR: Unknown worker argument.
exit /b 1
:service
python -m gateway.service
goto worker_done
:simulator
python -m gateway.sim run
goto worker_done
:web
python -m gateway.web
goto worker_done
:worker_done
set "WORKER_EXIT=%ERRORLEVEL%"
echo.
echo Process stopped with exit code %WORKER_EXIT%. Review output above.
exit /b %WORKER_EXIT%

:folder_error
echo ERROR: Cannot access the batch file's directory.
goto failed
:venv_error
echo ERROR: Missing or broken .venv. Create it and install requirements.txt first.
goto failed
:project_error
echo ERROR: Put this file beside the Compose file, gateway folder, and .venv.
goto failed
:launch_error
echo ERROR: Could not open a process window. Earlier windows may still be running.
:failed
echo.
echo Startup stopped. Review the error above.
pause
exit /b 1
