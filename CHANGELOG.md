# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/),
версии — [Semantic Versioning](https://semver.org/). Полный перечень изменений
форка — в [CHANGES_FORK.md](CHANGES_FORK.md).

## [Unreleased]

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
