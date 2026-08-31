@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat

if not exist "dev-cert\cert.pem" (
    echo No HTTPS certificate found in dev-cert\
    echo Run gen-cert.sh once via Git Bash first ^(needed for other devices' cameras to work^), then re-run this.
    pause
    exit /b 1
)

uvicorn app.main:app --reload --host 0.0.0.0 --port 8010 --ssl-keyfile dev-cert\key.pem --ssl-certfile dev-cert\cert.pem
pause
