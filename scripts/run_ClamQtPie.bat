@echo off
rem ClamQtPie を開発環境（clamav_frontend\.venv）から起動する。
rem 管理者で動かすときは、このファイルを右クリックして「管理者として実行」を選ぶ。
rem 引数はそのまま app.py に渡す。
setlocal
chcp 65001 >nul
cd /d "%~dp0..\clamav_frontend" || exit /b 1

set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo .venv が見つからないため、PATH の python で起動します。
    set "PYTHON=python"
)

"%PYTHON%" app.py %*
if errorlevel 1 pause
endlocal
