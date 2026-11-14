@echo off
cd /d E:\ESP32Project\WIFI-CSI\backend
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 18000 >> ..\backend_run.log 2>&1
