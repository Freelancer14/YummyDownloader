#!/usr/bin/env bash
# Запуск локального Telegram Bot API сервера для YummyBot (Linux-сервер).
# Поднимает лимит файла с 50 МБ до 2 ГБ. Это официальная программа Telegram
# (github.com/tdlib/telegram-bot-api) — тот же бот, тот же токен, просто свой шлюз.
#
# Перед первым запуском нужен бинарник telegram-bot-api:
#   sudo apt install telegram-bot-api        # если есть в репозитории дистрибутива
#   # либо собрать по инструкции с github.com/tdlib/telegram-bot-api
#
# Затем:  ./start_tg_api.sh       (бот запускай отдельно — start_bot_local.sh)

set -e

# --- credentials с my.telegram.org (нужны самому серверу для входа в сеть TG) ---
# Впиши свои значения ЛОКАЛЬНО (или экспортируй заранее в окружении).
# НЕ коммить реальные ключи в репозиторий.
export TELEGRAM_API_ID="${TELEGRAM_API_ID:-ВПИШИ_API_ID}"
export TELEGRAM_API_HASH="${TELEGRAM_API_HASH:-ВПИШИ_API_HASH}"

PORT=8744
# Уникальная рабочая директория — чтобы НЕ пересеклась с другими ботами/сессиями
# на сервере. В имя зашит owner id, такого больше ни у кого не будет.
WORKDIR="$HOME/tg-bot-api-yummy-8252748715"
mkdir -p "$WORKDIR"

echo "Локальный Bot API сервер: порт $PORT, данные в $WORKDIR"
exec telegram-bot-api \
    --api-id="$TELEGRAM_API_ID" \
    --api-hash="$TELEGRAM_API_HASH" \
    --local \
    --http-port="$PORT" \
    --dir="$WORKDIR" \
    --temp-dir="$WORKDIR/tmp" \
    --log="$WORKDIR/server.log"
