#!/usr/bin/env bash
# Запуск YummyBot в режиме локального Bot API сервера (лимит файла 2 ГБ).
# Сначала в ДРУГОМ терминале должен работать ./start_tg_api.sh
#
# Прокси для Telegram при локальном сервере НЕ нужен (сервер сам ходит наружу).
cd "$(dirname "$0")"

export YUMMY_BOT_TG_API="http://127.0.0.1:8744"   # адрес локального сервера
export YUMMY_BOT_PROXY=""                          # локальному серверу прокси не нужен

python3 yummy_anime_bot.py
