@echo off
rem ClamQtPie のトレイGUI を開発環境（clamav_frontend\.venv）から起動する（管理者の権限は要らない）。
rem 先にサービスを動かしておく（install_service.bat、または service_main.py --console）。
rem 引数はそのまま tray_main.py に渡す。
setlocal
chcp 65001 >nul
cd /d "%~dp0..\clamav_frontend" || exit /b 1

set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo .venv が見つからないため、PATH の python で起動します。
    set "PYTHON=python"
)

"%PYTHON%" tray_main.py %*
if errorlevel 1 pause
endlocal
