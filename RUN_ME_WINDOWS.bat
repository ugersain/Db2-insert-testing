@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  Db2 insert speed test - sets up and runs everything
echo ============================================================

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)

if "%~1"=="" (
  where docker >nul 2>nul
  if errorlevel 1 (
    echo Docker was not found. Install Docker Desktop from https://www.docker.com/products/docker-desktop/
    echo or use your own Db2:  RUN_ME_WINDOWS.bat "DATABASE=MYDB;HOSTNAME=host;PORT=50000;PROTOCOL=TCPIP;UID=user;PWD=pw;"
    pause
    exit /b 1
  )
  docker info >nul 2>nul
  if errorlevel 1 (
    echo Docker Desktop is installed but not running. Start it, wait until it says "running", then run this again.
    pause
    exit /b 1
  )
  echo Using a local Db2 in Docker. First run downloads about 3 GB and takes several minutes...
  powershell -NoProfile -Command "python -u local_test\run_local_test.py 2>&1 | Tee-Object -FilePath results.txt"
) else (
  echo Using your Db2. Only tables named ZZ_BENCH_* are created and dropped.
  powershell -NoProfile -Command "python -u local_test\run_local_test.py --dsn '%~1' 2>&1 | Tee-Object -FilePath results.txt"
)

echo.
echo Finished. The full output is saved in results.txt in this folder - send it to me.
pause
