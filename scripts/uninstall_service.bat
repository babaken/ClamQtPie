@echo off
rem install_service.bat で登録した ClamQtPie サービスを止めて、登録を削除する（開発中の確認用）。
rem 設定・データ・ログ（%ProgramData%\ClamQtPie）は残す。消すときは、そのフォルダを管理者で削除する（隔離したファイルも消える）。
setlocal
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0..\clamav_frontend"
set "PY=%CLAMQTPIE_PYTHON%"
if "%PY%"=="" if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if "%PY%"=="" set "PY=python"
"%PY%" service_main.py stop
"%PY%" service_main.py remove
pause
