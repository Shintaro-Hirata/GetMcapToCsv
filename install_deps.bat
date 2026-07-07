@echo off
setlocal EnableExtensions
cd /d %~dp0
title Install Dependencies

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
    echo - Python をインストールしてください（Windows版推奨）。
    echo - 既に入っているのに見つからない場合：PATHが通っていない可能性があります。
    pause
    exit /b 1
  )
)

echo [INFO] Using: %PY%
%PY% -c "import sys; print('[INFO] sys.executable=', sys.executable)"

REM ---- pip が動くか確認しつつインストール ----
%PY% -m pip --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo [ERROR] pip が見つかりません。
  echo - Python を pip も含めてインストールしているか確認してください。
  pause
  exit /b 1
)

echo.
echo [INFO] Installing requirements...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] pip install failed.
  echo - mcap-ros2idl-support の行で失敗する場合は README.md の
  echo   「mcap-ros2idl-support のインストール」を参照してください。
  pause
  exit /b 1
)

echo.
echo Done.
pause
