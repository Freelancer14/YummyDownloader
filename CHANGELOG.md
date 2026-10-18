# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/),
версии — [Semantic Versioning](https://semver.org/). Полный перечень изменений
форка — в [CHANGES_FORK.md](docs/CHANGES_FORK.md).

## [Unreleased]

## [1.0.6] — 2026-10-19

### Fixed

- Битые обрывки серий больше не пропускаются при resume: существующий файл
  на «уже есть - пропуск» теперь проверяется `hls_file_ok()` (ffmpeg открывает
  файл: нет `Duration` / есть `Invalid data` → неиграбельный обрывок).
  Сломанный файл удаляется и качается заново («повторно качаю: …»), вместо
  пропуска по порогу «больше 10 КБ», из-за которого получались битые mp4.
- Проверка итога качки добавлена и после скачивания (сегментный путь и прямой
  ffmpeg, CLI и GUI): неиграбельный файл не переименовывается в финальный,
  серия завершается ошибкой с причиной или качается заново.

## [1.0.5] — 2026-10-10

### Added

- Субтитры как полноценный вариант: `is_sub()`, даб в приоритете в
  `choose_dubbing()`, выбор саба → `_субтитры.txt` (`Субтитры: ...`),
  конфиг `allow_subtitles` / `preferred_subs`, CLI `--subs` / `--no-subs`,
  единый маркер `[саб]` для `.me` и `.tv`.
- GUI: сабы внизу списка с бейджем `[суб]`, чекбокс «Показывать субтитры»,
  подпись строки «Озвучка / субтитры:».

## [1.0.4] — 2026-10-04

### Fixed

- `.gitattributes`: `*.cmd/*.bat → eol=crlf` — ZIP с GitHub больше не ломает
  запуск (`cmd.exe` требует CRLF); все `.cmd` нормализованы в UTF-8 без BOM + CRLF.

## [1.0.3] — 2026-10-04

### Fixed

- `yummy_gui.py`: Ctrl+V/C/X/A при русской раскладке — маппинг по физическому
  keycode (VK-код не зависит от языка), keysym как запасной путь; чинен cut
  (позиция X = Ч `cyrillic_che`, а не Х).

## [1.0.2] — 2026-10-04

### Fixed

- `seg_dl.py`: склейка v2 — бинарный кат `.ts` + один ремукс вместо concat-демуксера;
  проверка TS sync-byte, `DISCONTINUITY/GAP` → fallback на прямой ffmpeg.
- `yummy_dl.py`: `--segs 0` форсирует прямой `ffmpeg` (диагностика склейки vs источника).

## [1.0.1] — 2026-10-04

### Fixed

- `seg_dl.py`: фризы на стыках сегментов — `concat_copy()` с `+genpts+igndts`,
  `avoid_negative_ts make_zero`, `aac_adtstoasc`; детект `EXT-X-KEY` / `EXT-X-MAP`
  с fallback на прямой `ffmpeg -c copy`.
- `*.cmd`: CRLF + UTF-8 без BOM (чинится запуск двойным кликом после ZIP с GitHub).

## [1.0.0] — 2026-10-03

Первый версионированный релиз форка (база — upstream
[foxycuter2-ai/YummyDownloader](https://github.com/foxycuter2-ai/YummyDownloader)).

### Added

- GUI `yummy_gui.py` (tkinter): ссылка → Проверить → озвучка/качество/серии/
  франшиза → Скачать/Отмена; прогресс серий и общий (МБ, МБ/с); спинбокс
  «Потоки серий» 1–8; кнопка «Установить зависимости»; фикс Ctrl+V в русской
  раскладке; докачка повторным «Скачать».
- `seg_dl.py`: параллельная качка HLS-сегментов (пул 8, до 32), ретраи 3x +
  backoff, urllib → curl-fallback, склейка `ffmpeg -f concat -c copy` без
  перекодирования, fallback на прямой `ffmpeg -c copy`.
- `tv_adapter.py`: поддержка `yummyanime.tv` — handshake карточки, слоты
  Alloha/Kodik, переводы × сезоны/серии serial-iframe Kodik, резолв серий
  через POST `/ftor`; авто-детект `.tv`-ссылок в CLI и GUI.
- `Setup_Python.cmd`: автопроверка/установка Python 3.9+ (winget → установщик
  python.org) + `pip install -r requirements.txt`; `Start_GUI.cmd`: запуск GUI
  в один клик.
- Версионирование: `VERSION`, `CHANGELOG.md`, переписанный `README.md`.

### Changed

- `yummy_dl.py`: параллельные серии (`workers=4`, `--workers 1–8`;
  `--segs` / `--seg-workers 1–32`); `download_hls()` с сегментным путём и
  прогресс-колбэком; `tv_grab()` для `.tv`; строка `потоки: серии=N, сегменты=M`.

### Known limitations

- Плееры Alloha/CVH не реализованы (только точка входа при handshake).
- `.tv` напрямую без VPN; при блокировке Kodik на egress нужны DIRECT-исключения.
- Франшиза (`all_seasons`) — только для `old.yummyani.me`.
- Потолок качества 720p, только Kodik — как в upstream.
