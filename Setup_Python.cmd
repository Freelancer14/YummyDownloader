@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title Установка Python для YummyDownloader
cd /d "%~dp0"

rem DRYRUN=1 — только показать ветки детекта, ничего не ставить и не качать.
if defined DRYRUN echo [DRYRUN] тестовый прогон, установка выполняться не будет.

echo ==============================================================
echo  YummyDownloader — проверка и установка Python
echo ==============================================================
echo.

set "PYCMD="
set "PYVER="

rem --- Шаг 1: ищем Python 3.9+ через лаунчер (от нового к старому) ---
for %%V in (3.12 3.11 3.10 3.9) do (
  if not defined PYCMD (
    py -%%V --version >nul 2>&1
    if !ERRORLEVEL! equ 0 (
      set "PYCMD=py -%%V"
      for /f "tokens=2" %%a in ('py -%%V --version 2^>^&1') do set "PYVER=%%a"
    )
  )
)

rem --- Шаг 2: запасные варианты — python / py с проверкой версии >= 3.9 ---
if not defined PYCMD (
  python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
  if !ERRORLEVEL! equ 0 (
    set "PYCMD=python"
    for /f "tokens=2" %%a in ('python --version 2^>^&1') do set "PYVER=%%a"
  )
)
if not defined PYCMD (
  py -c "import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
  if !ERRORLEVEL! equ 0 (
    set "PYCMD=py"
    for /f "tokens=2" %%a in ('py --version 2^>^&1') do set "PYVER=%%a"
  )
)

if defined PYCMD goto :have_python

echo [^!] Python 3.9+ не найден. Пробую установить Python 3.12...
echo.

rem --- Шаг 3: установка через winget (если есть) ---
where winget >nul 2>&1
if %ERRORLEVEL% equ 0 (
  echo [*] Ставлю через winget: Python.Python.3.12 ...
  if defined DRYRUN (
    echo [DRYRUN] winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  ) else (
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  )
  py -3.12 --version >nul 2>&1
  if !ERRORLEVEL! equ 0 (
    set "PYCMD=py -3.12"
    for /f "tokens=2" %%a in ('py -3.12 --version 2^>^&1') do set "PYVER=%%a"
    goto :have_python
  )
  echo [^!] winget не справился, пробую официальный установщик...
  echo.
) else (
  echo [*] winget не найден, качаю официальный установщик python.org...
  echo.
)

rem --- Шаг 4: официальный installer с python.org (первый живой URL побеждает) ---
set "PYTARGET=%LocalAppData%\Programs\Python\Python312"
set "INSTALLED="
for %%R in (3.12.10 3.12.9 3.12.8 3.12.7) do (
  if not defined INSTALLED (
    set "PYURL=https://www.python.org/ftp/python/%%R/python-%%R-amd64.exe"
    set "PYSETUP=%TEMP%\python-%%R-amd64.exe"
    echo [*] Пробую %%R ...
    if defined DRYRUN (
      echo [DRYRUN] Invoke-WebRequest -Uri '!PYURL!' -OutFile '!PYSETUP!'
      echo [DRYRUN] '!PYSETUP!' /quiet InstallAllUsers^=0 PrependPath^=1 Include_pip^=1 TargetDir^=!PYTARGET!
    ) else (
      powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri '!PYURL!' -OutFile '!PYSETUP!' } catch { exit 1 }"
      if !ERRORLEVEL! equ 0 (
        if exist "!PYSETUP!" (
          echo [*] Скачалось, ставлю тихо в !PYTARGET! ...
          "!PYSETUP!" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 TargetDir="!PYTARGET!"
          if exist "!PYTARGET!\python.exe" (
            set "PYCMD=!PYTARGET!\python.exe"
            for /f "tokens=2" %%a in ('"!PYTARGET!\python.exe" --version 2^>^&1') do set "PYVER=%%a"
            set "INSTALLED=1"
            del /q "!PYSETUP!" >nul 2>&1
          ) else (
            echo [^!] Установщик %%R не дал python.exe, пробую следующую версию...
          )
        )
      ) else (
        echo [*] %%R не скачался, пробую следующую версию...
      )
    )
  )
)
if defined DRYRUN goto :dryrun_end
if not defined PYCMD (
  echo.
  echo [X] Не смог поставить Python автоматически.
  echo     Поставь вручную с https://www.python.org/downloads/ ^(3.12, галочки pip + tcl/tk^)
  echo     и запусти этот файл ещё раз.
  pause
  exit /b 1
)

:have_python
echo [+] Найден Python %PYVER% ^(%PYCMD%^)
echo.
if defined DRYRUN goto :dryrun_end

rem --- Шаг 5: pip + зависимости проекта ---
echo [*] Обновляю pip...
%PYCMD% -m pip install --upgrade pip
if %ERRORLEVEL% neq 0 echo [^!] pip не обновился, продолжаю со старым...
echo.
echo [*] Ставлю зависимости из requirements.txt...
%PYCMD% -m pip install -r "%~dp0requirements.txt"
if %ERRORLEVEL% neq 0 (
  echo.
  echo [X] pip install завершился с ошибкой. Проверь интернет и запусти ещё раз.
  pause
  exit /b 1
)

echo.
echo ==============================================================
echo  [OK] Готово: Python %PYVER%, зависимости стоят.
echo  Дальше: Start_GUI.cmd — запуск окна, «Скачать аниме.cmd» — консоль.
echo ==============================================================
pause
exit /b 0

:dryrun_end
echo.
echo [DRYRUN] Итог детекта: PYCMD=%PYCMD% PYVER=%PYVER%
exit /b 0
