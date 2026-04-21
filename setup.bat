@echo off
REM One-click setup for local-speak on Windows.
setlocal
cd /d "%~dp0"

echo.
echo === local-speak setup ===
echo.

REM 1. Python 3.11 check
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo [X] Python 3.11 not found on PATH.
    echo     Download from https://www.python.org/downloads/release/python-3118/
    echo     Be sure to check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

REM 2. venv
if not exist .venv (
    echo Creating virtual environment with Python 3.11...
    py -3.11 -m venv .venv
    if errorlevel 1 (
        echo [X] Failed to create .venv
        pause
        exit /b 1
    )
)

echo Upgrading pip and wheel...
.venv\Scripts\python.exe -m pip install --upgrade pip wheel --quiet

REM 3. PyTorch (CUDA 12.8 wheels, ~3 GB)
echo.
echo Installing PyTorch (CUDA 12.8) -- this is ~3 GB, takes a few minutes...
.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 (
    echo [X] PyTorch install failed.
    pause
    exit /b 1
)

REM 4. Rest of the stack
echo.
echo Installing remaining packages...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] Package install failed.
    pause
    exit /b 1
)

REM 5. CUDA sanity check
echo.
echo Verifying CUDA...
.venv\Scripts\python.exe -c "import torch; ok=torch.cuda.is_available(); print('CUDA: ' + ('YES -- ' + torch.cuda.get_device_name(0) if ok else 'NO -- you need an NVIDIA GPU with a recent driver')); raise SystemExit(0 if ok else 1)"
if errorlevel 1 (
    echo.
    echo [!] CUDA not detected. Update your NVIDIA driver:
    echo     - Blackwell (50-series):  572 or newer
    echo     - Ada / Ampere:           555 or newer
    echo     Then re-run setup.bat.
    pause
    exit /b 1
)

echo.
echo === Setup complete ===
echo.
echo Launch options:
echo     run.vbs   Normal use -- silent, tray icon only
echo     run.bat   Debug      -- console visible with live logs
echo.
echo First launch will download the Whisper model (~800 MB). After that, launches are instant.
echo.
pause
