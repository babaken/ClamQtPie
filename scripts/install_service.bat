@echo off
rem ClamQtPie サービスを、開発中のソースから Windows サービスとして登録して開始する（開発中の確認用。配布物には入れない）。
rem 正式な導入方法（インストーラ）は段階 12-4 で作る。
rem 注意: サービスは SYSTEM で動き、このフォルダのソースをそのまま実行する。確認が終わったら
rem uninstall_service.bat で必ず削除すること。
rem Python は CLAMQTPIE_PYTHON、clamav_frontend\.venv、PATH の python の順に探す（run_ClamQtPie.bat と同じ）。
setlocal
net session >nul 2>&1
if errorlevel 1 (
    rem 管理者でなければ、UAC で昇格して自分を実行し直す
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0..\clamav_frontend"
set "PY=%CLAMQTPIE_PYTHON%"
if "%PY%"=="" if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
if "%PY%"=="" set "PY=python"
echo 登録します: "%PY%" service_main.py
"%PY%" service_main.py --startup delayed install
"%PY%" service_main.py start
sc query ClamQtPie
pause
