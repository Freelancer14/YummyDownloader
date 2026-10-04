@echo off
title Скачать аниме (YummyAnime)
cd /d "%~dp0"

:menu
cls
echo ==============================================================
echo            Качалка аниме с YummyAnime
echo ==============================================================
echo.
echo  Открой нужный тайтл на old.yummyani.me, скопируй ссылку из
echo  адресной строки (вида .../catalog/item/...) и вставь её.
echo  Дальше программа спросит озвучку, качество и серии.
echo.
set "URL="
set /p "URL=Вставь ссылку на тайтл (или Enter для выхода): "
if not defined URL goto :eof

py yummy_dl.py "%URL%"

echo.
echo --------------------------------------------------------------
echo  Папка: %~dp0downloads
echo --------------------------------------------------------------
echo.
set "AGAIN="
set /p "AGAIN=Скачать ещё один тайтл? (y/n): "
if /i "%AGAIN%"=="y" goto menu
