@echo off
title Phoenix Bridge Server
cd /d "%~dp0"
echo ============================================
echo   PHOENIX BRIDGE SERVER
echo   NT8 :8765 / Bots :8766 / Health :8767
echo ============================================
echo.
python bridge\bridge_server.py
pause
