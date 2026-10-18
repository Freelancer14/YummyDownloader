# Тесты парсеров

Юнит-тесты чистых парсеров YummyDownloader на реальных сохранённых ответах
сайтов. Сеть не используется — всё читается из `tests/fixtures/`.

## Запуск

```
python tests/run_tests.py                 # все тесты, подробный вывод
python tests/run_tests.py -q              # тихий режим
python tests/run_tests.py test_parsers    # один модуль
```

Код возврата 0 — зелёные, 1 — есть падения. Зависимостей нет, только stdlib
`unittest`; нужен Python 3.8+.

## Что покрыто

- `test_parsers.py` — разбор HTML/JSON по фикстурам: `parse_page_id` (17482),
  `groups_from_items` (14 озвучек / 142 серии / группа-сабы / нумерация с 1),
  `build_title_info`, `parse_sig` (6 sig-переменных + type/hash/id),
  `parse_translations` (10 переводов) и `parse_seasons` ({0: 4, 1: 11}, selected 1),
  `kodik_decode` на ответе `/ftor` (`links` ⊆ {360,480,720}, есть 720),
  карточка `.tv` (id 8383, 2 слота), слоты Alloha/Kodik, `pick_slot`.
- `test_adapters_contract.py` — `MeSite`/`TvSite` реализуют `SiteAdapter`,
  `get_adapter()` выбирает адаптер по URL и голому id, поля `TitleInfo`/
  `EpisodeRef`, `marker`/`is_sub`/`translation_label` на синтетике,
  `enum_to_title_info` для serial и одиночного фильма.
- Регресс-тесты: битый/пустой вход даёт пустую структуру или внятное
  исключение, а не молчаливый мусор (проверено для всех парсеров).

## Когда сайт поменялся

1. Перезаписать соответствующую фикстуру в `tests/fixtures/` (как снимали —
  см. `tests/fixtures/MANIFEST.md`).
2. Обновить числа в тестах, если состав страницы изменился (число озвучек,
   переводов, id тайтла).
3. Прогнать `python tests/run_tests.py` — зелёный прогон означает, что новый
   ответ сайта всё ещё разбирается.

Фикстуры 4/6/10 содержат подписи с ограниченным сроком жизни — для оффлайн-
парсинга это нормально, для живых запросов надо снимать заново.
