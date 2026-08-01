@echo off
title STRMhub

echo ============================================
echo            STRMhub  Starting...
echo ============================================
echo.

cd /d "%~dp0backend"

:: Find Python 3.12
set "PYTHON="
if exist "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe" (
    set "PYTHON=C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"
) else if exist "C:\Python312\python.exe" (
    set "PYTHON=C:\Python312\python.exe"
)

if "%PYTHON%"=="" (
    for /f "tokens=*" %%i in ('where python 2^>nul') do (
        set "PYTHON=%%i"
    )
)

if "%PYTHON%"=="" (
    echo [ERROR] Python not found, please install Python 3.12+
    pause
    exit /b 1
)

echo Python: %PYTHON%
"%PYTHON%" --version

:: Check dependencies
echo.
echo Checking dependencies...
"%PYTHON%" -c "import fastapi" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    "%PYTHON%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [ERROR] Install failed, please run manually: pip install -r requirements.txt
        pause
        exit /b 1
    )
    echo Dependencies installed.
    echo.
)

:: Start service
echo ============================================
echo  URL: http://localhost:8000
echo  Press Ctrl+C to stop
echo ============================================
echo.

start "" http://localhost:8000

"%PYTHON%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000

pause
