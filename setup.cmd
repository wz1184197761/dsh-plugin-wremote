@echo off
setlocal
cd /d "%~dp0"

echo === dsh-plugin-wremote / upstream service setup ===
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [x] Python 3.10+ not found on PATH.
  echo     Install it from https://www.python.org/downloads/windows/
  echo     and tick "Add python.exe to PATH", then run this script again.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] creating .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [x] failed to create the virtual environment
    exit /b 1
  )
) else (
  echo [1/3] .venv already exists, reusing it
)

echo [2/3] installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r wremote\requirements.txt --quiet
if errorlevel 1 (
  echo [x] dependency install failed
  exit /b 1
)

echo [3/3] starting the service on port 8098 ...
".venv\Scripts\python.exe" wremote\launch.py --port 8098 --fps 20 --quality 85
if errorlevel 1 (
  echo [x] the service did not come up, see wremote\server.log
  exit /b 1
)

echo.
echo Done. The capture service is listening on http://127.0.0.1:8098/
echo Open that URL in a browser to check the picture, then install the DSH plugin:
echo     dsh plugin add "%~dp0."
echo.
echo To keep the service alive across logins, point a scheduled task at:
echo     "%~dp0.venv\Scripts\python.exe" "%~dp0wremote\launch.py" --port 8098 --fps 20 --quality 85
echo.
pause
