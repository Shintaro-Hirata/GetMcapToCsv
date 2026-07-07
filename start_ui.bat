@echo off
setlocal EnableExtensions

REM ==========================================================
REM  ダブルクリックでも必ず「開いたまま」にする（cmd /k で再起動）
REM ==========================================================
if /i not "%~1"=="__RUN__" (
  start "GetMcapToCsv UI" cmd /k ""%~f0" __RUN__"
  exit /b
)

REM ---- 文字化け対策（UTF-8） ----
chcp 65001 >nul

title GetMcapToCsv UI
cd /d "%~dp0"

echo [INFO] Working dir: %cd%
echo.

REM ---- Python起動コマンドを決める（py優先、なければpython） ----
set "PY=py"
where py >nul 2>&1
if errorlevel 1 (
  set "PY=python"
  where python >nul 2>&1
  if errorlevel 1 (
    echo.
    echo [ERROR] Python launcher 'py' も 'python' も見つかりません。
    echo - Python をインストールしてください。
    echo - install_deps.bat を先に実行してください。
    echo.
    pause
    exit /b 1
  )
)

echo [INFO] Using: %PY%

echo.
echo [INFO] Starting Streamlit UI...
echo - ブラウザが自動で開きます（開かない場合は表示される URL を開いてください）
echo - 終了する時はこのウィンドウを閉じてください
echo.
%PY% -m streamlit run app.py
pause
