# YummyDownloader

Download anime episodes from **YummyAnime** (`old.yummyani.me` and mirrors) **without ever opening the player** — so no first-second previews, no thumbnails, **no spoilers**.

It talks to the site's internal API, pulls the direct stream from the Kodik player, and saves clean `.mp4` files into per-title folders — keeping a **single voiceover (dub)** of your choice.

> 🇷🇺 Русская версия — ниже / [Russian version below](#-русская-версия).

---

## Why

The usual way (browser "video download" extensions) forces you to **press play** to capture the stream — which flashes the opening seconds and a scrubber preview at you. For people who hate spoilers, that's a dealbreaker. YummyDownloader never plays anything: it resolves the HLS playlist directly and hands it to `ffmpeg`.

## Features

- 🚫 **No spoilers** — the player is never opened.
- 🎙️ **One dub, your pick** — interactive menu lists every voiceover actually available for the title (with episode counts).
- 📺 **Quality select** — max / 480 / 360.
- 🔢 **Episode ranges** — `5-12`, `3,7,9`, `5-`, or all.
- 🗂️ **Folders per title**, with a `_dub.txt` note recording which voiceover was used.
- 🧩 **Whole franchise** — optionally walk every season in the site's "viewing order".
- ⏯️ **Resumable** — already-downloaded episodes are skipped.
- 📦 **No system installs** — `ffmpeg` ships inside the `imageio-ffmpeg` pip package.

## Telegram bot (`yummy_anime_bot.py`)

Beyond the CLI there's a self-contained Telegram bot: it searches several sources
(YummyAnime/Kodik, AniLibria, Zona), downloads episodes and sends the video into
the chat. It also downloads direct **YouTube/TikTok** links, supports large files
(>50 MB) via a userbot + private storage channel, and has a `/share` mode to bundle
several titles into one shareable code.

**Secrets are never stored in code.** Copy the template and fill in your values:

```bash
cp bot_secrets.example.json bot_secrets.json   # this file is git-ignored
```

`bot_secrets.json` holds the bot token (from **@BotFather**), Telegram `api_id`/
`api_hash` (from **my.telegram.org**), the storage-channel id and, if used, the
userbot phone/2FA. Then run:

```bash
pip install certifi imageio-ffmpeg telethon cryptg python-socks
python3 yummy_anime_bot.py
```

> ⚠️ Never commit `bot_secrets.json`, `bot_config.json` or `*.session` files —
> they grant full access to your bot and Telegram account. They are already in
> `.gitignore`.

## Requirements

- Python 3.9+
- `pip install -r requirements.txt` (`certifi`, `imageio-ffmpeg`)
- Network access to `kodikplayer.com` and the CDN `solodcdn.com`.
  Some ISPs block the old `kodik.info` / `aniqit.com` domains via DNS — this tool only uses the live ones.

## Install

```bash
git clone https://github.com/foxycuter2-ai/YummyDownloader.git
cd YummyDownloader
pip install -r requirements.txt
cp config.example.json config.json   # optional: tweak defaults
```

## Usage

```bash
python yummy_dl.py "https://old.yummyani.me/catalog/item/...."
```

(Or run with no argument and paste the link when asked.)

You'll be prompted, in order: **voiceover → quality → episodes → whole franchise? → confirm**. Press Enter at any prompt to take the default.

Files land in `downloads/<Title>/Серия NN.mp4`.

### Windows one-click

Double-click **`Скачать аниме.cmd`** in the folder, paste the link, answer the prompts.

## Configuration — `config.json`

| Key | Meaning |
|---|---|
| `preferred_dubbings` | Priority list; the first one available for a title is pre-selected as default. e.g. `AniLibria`, `AniStar`, `Dream Cast`, `AnimeVost`, `AniDUB`, `AniMaunt`. |
| `quality` | `"max"` (≈720p), `"480"`, or `"360"`. |
| `all_seasons` | `true` to walk every season of the franchise. |
| `output_dir` | Where files go (default: `downloads/` next to the script). |
| `site` | Base site URL. |

## How it works

1. Read the numeric `page_id` from the title page.
2. `GET /api/anime/<id>/videos` → all episodes, dub names, Kodik iframe URLs.
3. For the chosen dub, fetch each Kodik iframe, POST to `/ftor`, and decode the obfuscated link (base64 + a variable letter-shift cipher — brute-forced over all 26 shifts, picking the result containing `m3u8`).
4. `ffmpeg -c copy` pulls the HLS stream into `.mp4`.

## Legal

For **personal/offline use** of content you're entitled to access. You are responsible for complying with YummyAnime's terms and your local copyright law. This project ships no content and is not affiliated with YummyAnime, Kodik, or any rights holder.

## License

[MIT](LICENSE)

---

## 🇷🇺 Русская версия

Качалка аниме с **YummyAnime** (`old.yummyani.me` и зеркала) **без запуска плеера** — значит, никаких превью первых секунд, никаких миниатюр, **никаких спойлеров**.

Программа сама дёргает внутренний API сайта, берёт прямой поток из плеера Kodik и складывает чистые `.mp4` по папкам, сохраняя **одну озвучку** на твой выбор.

### Зачем

Обычные браузерные «скачивалки видео» требуют **нажать play**, чтобы перехватить поток — а это показывает первые секунды и предпросмотр на перемотке. Кто не любит спойлеры — поймёт. Здесь плеер не запускается вообще: m3u8 достаётся напрямую и отдаётся `ffmpeg`.

### Возможности

- 🚫 **Без спойлеров** — плеер не открывается.
- 🎙️ **Одна озвучка** — меню показывает реально доступные для тайтла озвучки (с числом серий).
- 📺 **Выбор качества** — макс / 480 / 360.
- 🔢 **Диапазоны серий** — `5-12`, `3,7,9`, `5-` или все.
- 🗂️ **Папки по тайтлам** + файл `_озвучка.txt`.
- 🧩 **Вся франшиза** — по «порядку просмотра» с сайта.
- ⏯️ **Докачка** — уже скачанные серии пропускаются.
- 📦 **Без установок в систему** — `ffmpeg` внутри пакета `imageio-ffmpeg`.

### Установка

```bash
git clone https://github.com/foxycuter2-ai/YummyDownloader.git
cd YummyDownloader
pip install -r requirements.txt
```

### Запуск

```bash
python yummy_dl.py "https://old.yummyani.me/catalog/item/...."
```

Спросит по порядку: **озвучка → качество → серии → вся франшиза? → подтверждение**. Enter = значение по умолчанию.

**Windows в один клик:** двойной клик по **`Скачать аниме.cmd`**, вставь ссылку, отвечай на вопросы.

Файлы появятся в `downloads/<Название>/Серия NN.mp4`.

### Требования к сети

Нужен доступ к `kodikplayer.com` и CDN `solodcdn.com`. Старые домены `kodik.info` / `aniqit.com` у многих провайдеров блокируются по DNS — тут они не используются.

### Лицензия и оговорка

Для личного офлайн-использования контента, к которому у тебя есть легальный доступ. Соблюдение правил сайта и авторского права — на тебе. Проект не содержит контента и не связан с YummyAnime/Kodik. Лицензия [MIT](LICENSE).
