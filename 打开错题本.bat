@echo off
chcp 65001 >nul
title SQL 错题本
cd /d "%~dp0"

rem ============ 自动探测 Python（防止命中 Windows 商店占位符，导致服务起不来） ============
set "PYCMD="
rem ① 优先常见真实安装路径，命中即用完整路径（最稳）
for %%p in (
  "%USERPROFILE%\miniconda3\python.exe"
  "%USERPROFILE%\anaconda3\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
  "C:\Python311\python.exe"
) do if not defined PYCMD if exist "%%~p" set "PYCMD=%%~p"

rem ② 兜底：PATH 里的 python（排除 Windows 商店占位符）
if not defined PYCMD (
  where python 2>nul | findstr /i /v "WindowsApps" >nul
  if not errorlevel 1 set "PYCMD=python"
)

if not defined PYCMD (
  echo [错误] 未找到可用的 Python。
  echo   检测到本机装有 Miniconda，但当前 cmd 里 python 指向的是 Windows 商店占位符。
  echo   修复方法（任选其一）：
  echo   1. 打开「Anaconda Prompt」，运行：conda init  然后重新打开 cmd；
  echo   2. 在 cmd 中运行：setx PATH "%USERPROFILE%\miniconda3;%PATH%"
  echo   3. 或直接安装 Python 3：https://www.python.org/downloads/
  echo   完成后重新双击本文件即可。
  pause
  exit /b 1
)
echo [..] 使用 Python：%PYCMD%

echo [1/3] 重建索引...
"%PYCMD%" scripts\rebuild_index.py

echo [2/3] 检查并启动本地 AI 服务...
powershell -NoProfile -Command "try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',8765);$c.Close();'up'}catch{'down'}" > "%TEMP%\sqlqk_port.txt" 2>nul
set /p PSTAT=<"%TEMP%\sqlqk_port.txt"
del "%TEMP%\sqlqk_port.txt" >nul 2>nul
if "%PSTAT%"=="up" (
  echo [..] 服务已在运行，跳过启动。
) else (
  echo [..] 正在启动本地服务，请稍候...
  start "SQL错题本-AI服务" /min "%PYCMD%" scripts\server.py
  timeout /t 2 >nul
)

echo [3/3] 打开错题本...
start "" "http://127.0.0.1:8765/index.html"
