# Изменения форка относительно upstream

Upstream: https://github.com/foxycuter2-ai/YummyDownloader
База форка — распакованный zip upstream (каталог без `.git`, сравнение по коду).

## Кратко

Upstream качает последовательно: один `ffmpeg -c copy` на серию, серии строго
по очереди, только плеер Kodik, потолок 720p, только интерактивный CLI.
Форк добавляет скорость (параллельные серии + параллельные сегменты) и GUI,
не меняя качество (max untouched) и не ломая CLI.

## Добавлено

### 1. `yummy_gui.py` (новый, ~55 КБ, tkinter, только стандартная библиотека)
- Окно: ссылка → «Проверить» → озвучка / качество / серии / франшиза →
  «Скачать» / «Отмена».
- Состояния: idle / checking / ready / downloading / done / error / stopped.
- Фикс русской раскладки: Ctrl+V/C/X/A через маппинг keysym
  (`Cyrillic_em` и т.д.) + контекстное меню по ПКМ + Shift+Insert.
- Прогресс: `ffmpeg -progress pipe:1`, класс `SpeedTracker` (скользящее окно
  3 с), очередь событий + `root.after(100)`, строки серий со своими барами,
  общий бар, счётчики МБ и скорости.
- Спинбокс «Потоки серий» 1–8 (дефолт из конфига), повтор упавших серий —
  повторным нажатием «Скачать» (готовые подхватываются как «уже есть»).
- GUI качает тем же конвейером, что CLI (сегменты → fallback ffmpeg),
  прогресс сегментного пути — по готовым сегментам/total.

### 2. `seg_dl.py` (новый, ~12 КБ)
Параллельная качка HLS-сегментов пулом потоков (дефолт 8):
- `resolve_media()` — мастер-плейлист → вариант с максимальным BANDWIDTH.
- `parse_segments()` — EXTINF-плейлист → URL сегментов по порядку.
- `download_one()` — ретраи 3x + backoff, сначала urllib (уважает прокси
  из env), при неудаче — `curl` через subprocess (переживает 302-redirect
  `cloud.solodcdn.com` → `nova.cloud.solodcdn.com`, на котором urllib падает).
- `download_segments()` — `ThreadPoolExecutor`, файлы `seg-%05d.ts`, порядок
  гарантирован именами, прогресс-колбэк под lock.
- `concat_copy()` — склейка `ffmpeg -f concat -safe 0 -c copy`, **без
  перекодирования** (битрейт untouched). Не вышла → `SegError`, вызывающий
  код идёт fallback'ом на прямой `ffmpeg -c copy` с m3u8.
- Временные каталоги `<серия>.segs` всегда удаляются; остановка через
  `threading.Event`/`callable` → исключение `Stopped`.

## Изменено

### 3. `yummy_dl.py` (~20 КБ)
- `DEFAULT_CONFIG`: `workers=4` (параллельных серий), `seg_workers=8`
  (параллельных сегментов). Задаются также через `config.json`.
- `grab_anime()`: цикл `for num in nums` заменён на `ThreadPoolExecutor`
  (`min(workers, len(todo))`); новая `_download_episode()`. Ошибка одной
  серии не валит остальные; resume («уже есть — пропуск») до сабмита;
  результаты печатаются отсортированными по номеру — одна строка на серию
  в прежнем формате; `sleep 0.5` только при 1 воркере; строка
  `потоки: серии=N, сегменты=M`.
- `download_hls()`: новые опциональные параметры `progress_cb`, `is_stopped`,
  `seg_workers`. Сначала сегментный путь (`seg_dl`), `Stopped` пробрасывается,
  любая другая ошибка → одна строка в лог + старый путь без изменений.
- `__main__`: флаги `--workers N` (1–8), `--segs` / `--seg-workers N` (1–32),
  остальной CLI (интерактивное меню) без изменений.

## Не тронуто

- Качество: `max` по умолчанию, выбор max/480/360, потолок 720p — как в upstream.
- Только плеер Kodik (`only_kodik=True`); Alloha/CVH по-прежнему пропускаются.
- Разбор Kodik (`/ftor`, декод ссылок), API сайта, `sanitize`, раскладка
  `downloads/`, `requirements.txt` (`certifi` + `imageio-ffmpeg`, новых
  зависимостей нет).
- Совместимость CLI: старые запуски `py yummy_dl.py "<ссылка>"` работают,
  новые параметры опциональны.

## Опционально, вне репозитория

Наблюдение замеров: egress `103.114.43.184` — Kodik отдаёт 500 («Видео
запрещено к просмотру в данной стране»), напрямую CDN-сегменты не ходят.
Рабочая схема — прокси `http://192.168.1.130:7890` (mihomo) + 6 правил
`DOMAIN-SUFFIX ... DIRECT`: `kodikplayer.com`, `kodik.cc`, `solodcdn.com`,
`yummyani.me`, `yani.tv`, `aniliberty.top`. Настройка сервера, к коду
отношения не имеет, при другом egress может не понадобиться.

## Запуск

```bat
py yummy_dl.py "https://old.yummyani.me/catalog/item/..."
py yummy_dl.py --workers 4 --segs 8 "https://old.yummyani.me/catalog/item/..."
py yummy_gui.py
```

## yummyanime.tv (адаптер поверх Kodik-пайплайна)

### 4. `tv_adapter.py` (новый)
Handshake `.tv` (Chrome UA + cookie-jar, timeout 30, ретраи 2x, 404 не
ретраится; прокси из env не форсится — ходим напрямую):
- `tv_slots(card_url) -> [{slot, title, kind, src}]`: GET карточки
  (`data-player-anime-id` + `[data-player-slot/title]`) → GET
  `/engine/ajax/controller.php?mod=xfp` (Referer карточки) → GET
  `mod=player&id=<id>&slot=N` (Referer xfp + X-Requested-With) → JSON
  `{data:{kind,src}}`. id — из URL `/<id>-<slug>.html`, fallback —
  `data-player-anime-id`.
- `tv_kodik_iframes(card_url) -> [(title, src)]`: фильтр `kind=iframe` +
  `kodikplayer.com` в src.
- `tv_card_meta()`: заголовок (`<h1>` → og:title → `<title>` → slug).
- `SerialSession` + `parse_translations/parse_seasons/parse_sig`:
  Kodik serial-iframe — это вся ветка серий в одной странице: переводы —
  `<option data-media-id/hash/type/title/episode-count>`, сезоны/серии —
  `<div class="series-options"> → <div class="season-N">` с
  `<option data-id data-hash data-title="N серия">`. Сервер отдаёт
  запрошенный перевод только при живых kodik-cookie + Referer
  kodik-страницы (иначе default) — сессия это держит и сверяет
  `translationId`, иначе перевод пропускается с варнингом.
- Серия резолвится POST `/ftor` `{type:'seria', hash, id + sig-поля}`
  (см. `yummy_dl.tv_m3u8` поверх существующих `http_post/kodik_decode`).

### 5. Тонкие ветки `yummy_dl.py` / `yummy_gui.py`
- `is_tv_url()` + авто-детект в `grab()`: ссылка с `yummyanime.tv` идёт в
  `tv_grab()` (интерактив: озвучка → качество → серии, без франшизы).
- `tv_enumerate()` (все переводы × сезоны/серии + sig), `tv_grab_anime()`
  (тот же пул серий/сегментов, что в `grab_anime`), `_озвучка.txt`:
  `title / Озвучка: <перевод> / Плеер: <title слота> (yummyanime.tv) /
  Источник: <card_url>`. `.Subtitles` в именах → ` (Субтитры)`.
- Несериальный Kodik-iframe (фильм) — одна «Серия 01» штатным `kodik_m3u8`.
- GUI: `validate_url` уже пропускает `.tv`; `check_worker` строит те же
  `check_done` (слоты = список озвучек, `franchise_count=0` + `tv_enum`),
  `on_start`/`download_worker`/`_gui_one_ep` понимают tv-источники
  `('tv', sig, id, hash, ref)`.

### Живой тест (нукитаси `8383-nukitasi-d1`, без VPN, видео не качали)
Слоты: `0 «Плеер Alloha» iframe absciss.thealloha.club`, `1 «Плеер Kodik»
iframe kodikplayer.com/serial/68923/…/720p`, слоты 2+ → 404. Переводов 10
(AniJoy/AniStar/Beloved/Dream Cast/SHIZA/ТО Дубляжная/AniJoy-суб/KALGAZM/
SubVost-суб/KoeKak), дефолт по конфигу — AniStar. Ep1 → m3u8 720p да,
мастер-плейлист разобран: 237 сегментов. `_озвучка.txt` пишется реальным
проходом `tv_grab_anime`.

### Ограничения (зафиксировано)
- Alloha/CVH: только точка входа (`kind`/`src` слота видны), реализации нет.
- Качается выбранный сезон serial-страницы (selected, иначе максимальный);
  нулевой сезон (спешлы, тут 4 эп.) по умолчанию не берётся.
- CLI франшизы (`all_seasons`) к `.tv` неприменим — у карточки нет
  `viewing_order`; чекбокс GUI для `.tv` пуст (franchise_count=0).
- Serial дал все 11 серий — кейс «только 1 m3u8» не встретился; fallback
  single-iframe (`kodik_m3u8`) на месте.
- `py -m py_compile tv_adapter.py yummy_dl.py yummy_gui.py seg_dl.py` — ok.

## Проверка

```bat
py -m py_compile yummy_dl.py seg_dl.py yummy_gui.py
```

Замеры (AniLibria 720p, 237 сегментов ~6 с / ~0.5 МБ, труба ~95 КБ/с,
TTFB 0.4–0.9 с): чистый HTTP ~0.76 Мбит/с vs `ffmpeg -t 30 -c copy`
~0.64 Мбит/с — overhead ffmpeg минимален, ботлнек в трубе, выигрыш дают
параллелизм и 480p при необходимости.
