@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title YummyDownloader — окно
cd /d "%~dp0"

rem DRYRUN=1 — только показать, что было бы запущено.

set "PYCMD="
for %%V in (3.12 3.11 3.10 3.9) do (
  if not defined PYCMD (
    py -%%V -c "import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
    if !ERRORLEVEL! equ 0 set "PYCMD=py -%%V"
  )
)
if not defined PYCMD (
  python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
  if !ERRORLEVEL! equ 0 set "PYCMD=python"
)

if not defined PYCMD (
  echo [^!] Python 3.9+ не найден. Запускаю установку...
  echo.
  if defined DRYRUN (
    echo [DRYRUN] вызвал бы: call Setup_Python.cmd
    exit /b 0
  )
  call "%~dp0Setup_Python.cmd"
  if %ERRORLEVEL% neq 0 (
    echo [X] Установка не удалась, окно не запустить.
    pause
    exit /b 1
  )
  rem переопределяем после установки
  set "PYCMD="
  for %%V in (3.12 3.11 3.10 3.9) do (
    if not defined PYCMD (
      py -%%V --version >nul 2>&1
      if !ERRORLEVEL! equ 0 set "PYCMD=py -%%V"
    )
  )
  if not defined PYCMD if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    set "PYCMD=%LocalAppData%\Programs\Python\Python312\python.exe"
  )
  if not defined PYCMD (
    echo [X] Python поставился, но не находится. Открой новое окно cmd и попробуй ещё раз.
    pause
    exit /b 1
  )
)

if defined DRYRUN (
  echo [DRYRUN] запустил бы: %PYCMD% yummy_gui.py
  exit /b 0
)

echo [*] Запускаю окно: %PYCMD% yummy_gui.py
echo.
%PYCMD% yummy_gui.py
if %ERRORLEVEL% neq 0 (
  echo.
  echo [X] Окно закрылось с ошибкой ^(код %ERRORLEVEL%^).
  echo     Нет tkinter? Поставь Python с python.org ^(компонент tcl/tk^).
  echo     Нет модулей? Запусти Setup_Python.cmd.
  pause
  exit /b 1
)
