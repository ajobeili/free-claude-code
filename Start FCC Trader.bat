@echo off
REM FCC Trader — self-contained launcher with bundled Python.
REM Extract anywhere on Windows, double-click, done. No Python install needed.

setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"

REM --- Step 1: Find Python (bundled first, then system) ---------------
set "PYTHON="

REM 1a. Bundled portable Python (ships with the zip)
if exist "%SCRIPT_DIR%python-portable\python.exe" (
    set "PYTHON=%SCRIPT_DIR%python-portable\python.exe"
)

REM 1b. Local .venv
if not defined PYTHON (
    if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
        set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
    )
)

REM 1c. System PATH
if not defined PYTHON (
    for %%P in (python python3 py) do (
        if not defined PYTHON (
            %%P --version >nul 2>&1
            if not errorlevel 1 set "PYTHON=%%P"
        )
    )
)

REM 1d. Known system install paths
if not defined PYTHON (
    for %%D in (
        "C:\Users\ali.jobeily\AppData\Local\Programs\Python\Python313"
        "C:\Users\ali.jobeily\AppData\Local\Programs\Python\Python312"
        "C:\Users\ali.jobeily\AppData\Local\Programs\Python\Python311"
        "C:\Users\ali.jobeily\AppData\Local\Programs\Python\Python310"
        "C:\Program Files\Python312"
        "C:\Program Files\Python311"
    ) do (
        if not defined PYTHON (
            if exist "%%~D\python.exe" set "PYTHON=%%~D\python.exe"
        )
    )
)

REM --- Step 2: Error if still not found -------------------------------
if not defined PYTHON (
    echo.
    echo ERROR: Python not found.
    echo If you extracted from a zip, the python-portable folder is missing or incomplete.
    echo Re-extract the original zip file.
    pause
    exit /b 1
)

REM --- Step 3: Verify dependencies are installed -----------------------
"%PYTHON%" -c "import fastapi, uvicorn, httpx" 2>nul
if errorlevel 1 (
    if exist "%SCRIPT_DIR%python-portable\python.exe" (
        echo Installing dependencies into portable Python...
        "%PYTHON%" -m pip install --quiet fastapi uvicorn httpx python-dotenv
    ) else (
        echo Installing dependencies...
        "%PYTHON%" -m pip install --quiet fastapi uvicorn httpx python-dotenv
    )
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies.
        echo Run manually: "%PYTHON%" -m pip install fastapi uvicorn httpx python-dotenv
        pause
        exit /b 1
    )
)

REM --- Step 4: Launch -------------------------------------------------
cd /d "%SCRIPT_DIR%"
start "FCC Trader Server" "%PYTHON%" -m tools.scan_server
timeout /t 5 /nobreak > nul
start "" "http://localhost:8081"
