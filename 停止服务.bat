@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist server.pid (
  set /p SPID=<server.pid
  taskkill /PID %SPID% /F >nul 2>nul
  del server.pid >nul 2>nul
  echo 服务已停止。
) else (
  echo 未检测到运行中的服务。
)
pause
