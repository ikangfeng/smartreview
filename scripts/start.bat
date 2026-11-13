@echo off
rem ===== SmartReview one-click start (Windows) =====
rem Usage: configure backend/.env first (see backend/.env.example), then run this script.

set "ROOT=%~dp0.."

echo [1/3] Starting backend FastAPI on port 8002 ...
start "SmartReview Backend" cmd /k "cd /d %ROOT%\backend && python -m uvicorn main:app --host 0.0.0.0 --port 8002"

echo [2/3] Installing frontend dependencies (first run only) ...
cd /d "%ROOT%\frontend"
if not exist node_modules (
    call npm install
)

echo [3/3] Starting frontend Vite on port 5174 ...
call npm run dev
