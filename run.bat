@echo off
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%.venv\Scripts\pythonw.exe" (
  echo [ERROR] Virtual environment not found.
  echo Run scripts\setup.ps1 first.
  pause
  exit /b 1
)
start "" "%ROOT%.venv\Scripts\pythonw.exe" -B "%ROOT%app\video_tool.py"

