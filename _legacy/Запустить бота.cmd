@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запуск Telegram-бота YummyBot...
echo (закрой это окно, чтобы остановить бота)
echo.
py yummy_bot.py
echo.
echo Бот остановлен.
pause
