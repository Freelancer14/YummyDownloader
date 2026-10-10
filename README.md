# YummyDownloader — форк (GUI + параллельная качка + yummyanime.tv)

![version](https://img.shields.io/badge/version-1.0.5-blue) `VERSION: 1.0.5` · см. [CHANGELOG.md](CHANGELOG.md)

> **English summary:** Fork of [foxycuter2-ai/YummyDownloader](https://github.com/foxycuter2-ai/YummyDownloader) with a tkinter GUI, parallel episode/segment downloads, and `yummyanime.tv` support via the same Kodik pipeline. Quality stays `max untouched` (no re-encoding).

Форк оригинального [foxycuter2-ai/YummyDownloader](https://github.com/foxycuter2-ai/YummyDownloader):
качалка аниме **без запуска плеера** (значит, без спойлеров-превью) — напрямую забирает
HLS-поток из плеера Kodik и складывает чистые `.mp4` по папкам, по одной озвучке на выбор.

## Чем отличается от upstream

- **GUI** (`yummy_gui.py`, tkinter, только стандартная библиотека): ссылка → «Проверить» →
  озвучка / качество / серии / франшиза → «Скачать» / «Отмена». Прогресс по сериям и общий
  (МБ, МБ/с), спинбокс «Потоки серий» 1–8, кнопка «Установить зависимости», фикс вставки
  по Ctrl+V в русской раскладке.
- **Скорость** (`seg_dl.py` + правки `yummy_dl.py`): параллельные серии (`workers=4` по умолчанию,
  флаги `--workers/--segs`) + параллельные сегменты внутри серии (пул 8, до 32). Склейка
  `ffmpeg -f concat -c copy` **без перекодирования** с пересчетом меток (`+genpts+igndts`,
  `avoid_negative_ts make_zero`, `aac_adtstoasc` — фикс фризов в 1.0.1+); при ошибке — fallback на прямой `ffmpeg -c copy`.
- **`yummyanime.tv`** (`tv_adapter.py`): авто-детект ссылок `.tv`, handshake слотов (Alloha/Kodik),
  переводы × сезоны serial-страницы Kodik, качка тем же конвейером (сегменты → fallback).
- **Установка в 1 клик**: `Setup_Python.cmd` (автопроверка/установка Python 3.9+ + `requirements.txt`),
  `Start_GUI.cmd` (запуск окна).
- Качество не менялось: `max untouched` (потолок 720p), только плеер Kodik, CLI-совместимость сохранена.
- Подробнее — [CHANGES_FORK.md](docs/CHANGES_FORK.md) (полный список) и [CHANGELOG.md](CHANGELOG.md) (версии).

## Быстрый старт (Windows)

1. Двойной клик по **`Setup_Python.cmd`** — проверит Python 3.9+ (поставит 3.12 при отсутствии)
   и установит зависимости из `requirements.txt`.
2. Двойной клик по **`Start_GUI.cmd`** — откроется окно.

Консольный вариант без GUI: `«Скачать аниме.cmd»` (только `old.yummyani.me`, интерактивные вопросы).

## Использование GUI

1. Вставь ссылку (`old.yummyani.me/...` или `yummyanime.tv/...`), нажми **«Проверить»**.
2. Выбери озвучку, качество (`max`/480/360), серии (`5-12`, `3,7,9`, `5-` или все),
   при необходимости — франшизу (только для `.me`).
3. **«Скачать»** — файлы лягут в `downloads/<Название>/Серия NN.mp4` + `_озвучка.txt`.
4. **«Отмена»** останавливает качку; повторное «Скачать» докачивает упавшие серии
   (готовые подхватываются как «уже есть»).

## Использование CLI

```bat
py yummy_dl.py "https://old.yummyani.me/catalog/item/..."
py yummy_dl.py --workers 4 --segs 8 "https://old.yummyani.me/catalog/item/..."
py yummy_dl.py "https://yummyanime.tv/1234-nazvanie.html"
py yummy_gui.py
```

Вопросы по порядку: **озвучка → качество → серии → вся франшиза? (.me) → подтверждение**,
Enter = значение по умолчанию. Одна строка на серию в прежнем формате + строка
`потоки: серии=N, сегменты=M`.

## Источники

| Источник | Как работает |
|---|---|
| `old.yummyani.me` (+ зеркала) | Внутренний API `/api/anime/<id>/videos` → Kodik iframe → POST `/ftor` → m3u8. Есть франшиза («порядок просмотра»). |
| `yummyanime.tv` | `tv_adapter.py`: handshake карточки → слоты плееров → serial-iframe Kodik → переводы × сезоны/серии → POST `/ftor` → m3u8 тем же конвейером. Детект автоматический. |

## Параметры потоков

| Параметр | Где | Дефолт | Диапазон |
|---|---|---|---|
| `workers` (параллельных серий) | `config.json`, `--workers N`, спинбокс GUI | 4 | 1–8 |
| `seg_workers` (параллельных сегментов) | `config.json`, `--segs N` / `--seg-workers N` | 8 | 1–32 |

Склейка сегментов — `ffmpeg -c copy`, без перекодирования (битрейт untouched).
Проверка синтаксиса: `py -m py_compile yummy_dl.py seg_dl.py yummy_gui.py tv_adapter.py`.

> **1.0.1 — фикс фризов:** склейка `.ts`-сегментов рвала PTS/DTS на стыках. Лечение:
> `concat` с `-fflags +genpts+igndts -avoid_negative_ts make_zero -bsf:a aac_adtstoasc`.
> Шифрованные (`#EXT-X-KEY:AES-128`) и fmp4 (`#EXT-X-MAP`) плейлисты сегментным путем
> не качаем — сразу fallback на прямой `ffmpeg -c copy`. Битые серии перекачай
> (удали `Серия NN.mp4` и нажми «Скачать»).
>
> **1.0.2:** склейка v2 — бинарный кат `.ts` в один `combined.ts` + один ремукс
> (`+genpts+discardcorrupt`), проверка sync-byte `0x47`, `DISCONTINUITY/GAP` тоже
> уходят в прямой `ffmpeg` с нативным HLS-демуксером. Диагностика:
> `py yummy_dl.py --segs 0 "<ссылка>"` — форсирует прямой путь; если на нем чисто,
> а на сегментном нет — виновата склейка, кидай лог строки `(сегменты не вышли: ...)`.
> Битые серии перед тестом удали.

## Требования

- Python 3.9+ (поставит `Setup_Python.cmd`), пакеты `certifi` + `imageio-ffmpeg`
  (`pip install -r requirements.txt`; `ffmpeg` отдельно не нужен — он внутри `imageio-ffmpeg`).
- Сеть: доступ к `kodikplayer.com` и CDN `solodcdn.com`. Без VPN хождение напрямую;
  **с VPN для Kodik нужны DIRECT-исключения** (пример для mihomo — 6 правил `DOMAIN-SUFFIX ... DIRECT`:
  `kodikplayer.com`, `kodik.cc`, `solodcdn.com`, `yummyani.me`, `yani.tv`, `aniliberty.top`).
- GUI нужен `tcl/tk` (идёт с официальным установщиком python.org).

## Известные ограничения

- **Alloha/CVH не реализованы** — слот плеера виден при handshake `.tv`, но качается только Kodik.
- **`.tv` без VPN**: напрямую работает; через egress с блокировкой Kodik отдаёт 500 —
  лечится прокси + DIRECT-исключениями (см. выше), настройка сервера к коду отношения не имеет.
- **Франшиза только для `.me`** — у карточки `.tv` нет `viewing_order`; чекбокс GUI для `.tv` пуст.
- Для `.tv` качается выбранный сезон serial-страницы (selected, иначе максимальный);
  нулевой сезон (спешлы) по умолчанию не берётся.
- Потолок качества 720p — как в upstream.

## Конфигурация — `config.json`

Скопируй `config.example.json` в `config.json`. Ключи: `preferred_dubbings` (приоритет озвучек),
`quality` (`max`/480/360), `all_seasons` (франшиза `.me`), `output_dir`, `site`,
плюс `workers` / `seg_workers` (см. таблицу выше; `seg_workers: 0` — прямой ffmpeg без сегментов).

## Структура папки (GUI-only)

- Корень — только GUI: `yummy_gui.py`, ядро `yummy_dl.py` / `seg_dl.py` / `tv_adapter.py`,
  `Start_GUI.cmd`, `Setup_Python.cmd`, `requirements.txt`, `config.example.json`,
  `README.md`, `CHANGELOG.md`, `VERSION`, `LICENSE`.
- `docs/` — `CHANGES_FORK.md` (история форка).
- `_legacy/` — бот (`yummy_bot.py`, `yummy_anime_bot.py`), `link_replacer.py`,
  консольный запуск и `.sh`-скрипты. GUI их не использует.

> ZIP с GitHub: `.cmd` должны лежать с CRLF (иначе `cmd.exe` рвет строки).
> `.gitattributes` фиксирует `*.cmd/*.bat → eol=crlf`, так что свежий архив
> запускается двойным кликом без ошибок `'...' is not recognized`.

## Правовая оговорка

Для личного офлайн-использования контента, к которому есть легальный доступ. Соблюдение правил
сайтов и авторского права — на пользователе. Проект не содержит контента и не связан
с YummyAnime / yummyanime.tv / Kodik / Alloha. Лицензия [MIT](LICENSE).
