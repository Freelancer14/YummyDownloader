# Фикстуры парсеров — реальные сохранённые ответы сайтов

Все файлы — буквальные ответы сети, снятые **2026-10-19** без VPN, напрямую
(прокси из env не форсился). Кодировка UTF-8, без BOM. Видеофайлы не качались —
только HTML/JSON. Файлы крупнее 3 МБ потребовали бы обрезки; максимальный
здесь 144 КБ, так что все сохранены целиком и парсятся как есть.

## Образцы

| Сайт | Тайтл | page_id / id |
| --- | --- | --- |
| `.me` (old.yummyani.me) | Маг воды: Центральные провинции | 17482 |
| `.tv` (yummyanime.tv) | Нукитаси D1 | 8383 |

Колонка «жуёт» указывает на текущую раскладку модулей: `core.py` (http/api,
kodik_decode, kodik_m3u8, tv_m3u8), `site_me.py` (resolve_page_id, kodik_groups),
`tv_adapter.py` (handshake `.tv` и все `parse_*` для Kodik-страниц).
Названия функций те же, что и до рефакторинга, — менялись только файлы.

## Файлы

### 1. `me_title_page.html` — 61 378 Б

- **URL:** `https://old.yummyani.me/catalog/item/mag-vody`
- **Получен:** GET, `User-Agent` Firefox 120, `Referer: https://old.yummyani.me/`
- **Содержит:** полный HTML карточки тайтла; ключевое — `<meta ... page_id" content="17482">`,
  плюс `og:title`, хлебные крошки, блок related/сезонов.
- **Жуёт:** `resolve_page_id()` — `site_me.py` (реэкспорт в `yummy_dl.py`),
  regex `page_id"\s*content="(\d+)"`.

### 2. `me_api_anime.json` — 5 211 Б

- **URL:** `GET https://old.yummyani.me/api/anime/17482`
- **Заголовки:** `X-Application: wawegr8j13it4rdw`, `Vary: json`, `Lang: ru`, `Accept: application/json`
- **Содержит:** сырое тело ответа — один ключ `response` (dict, 32 поля):
  `title`, `description`, `poster`, `anime_url`, `anime_id`, `rating`, `genres`,
  `year`, `season`, `anime_status`, `type` (`{name, value, shortname, alias}`),
  `remote_ids`, `translates`, `creators`, `studios`, `viewing_order`,
  `episodes`, счётчики и т.д.
- **Жуёт:** `core.api("/anime/<id>")` → `json.loads(raw)["response"]`;
  дальше `yummy_dl.grab_anime()` (`info.get("title")`).
- **Тестам:** разворачивать ключ `response` так же, как это делает `api()`.

### 3. `me_api_videos.json` — 147 249 Б

- **URL:** `GET https://old.yummyani.me/api/anime/17482/videos`
- **Заголовки:** те же, что в п.2
- **Содержит:** сырое тело — `response` = список **260** записей.
  Поля каждой: `video_id`, `data`, `author`, `number`, `date`, `iframe_url`,
  `index`, `skips`, `views`, `duration`.
  Разбивка по хостам `iframe_url`: **kodikplayer.com — 142**,
  alloha.yani.tv — 36, ru.yummyani.me — 82.
  Из Kodik-ветки `kodik_groups()` даёт **14 озвучек / 142 серии**
  (AniLibria, AnimeVost, Dream Cast, ТО Дубляжная, AniStar, StudioBand Junior,
  AniBaza, Оканэ, Рабочая партия, KRAMSAI, Licoforice, BeeSound, DubClub —
  и сабы `[саб] Crunchyroll`).
- **Жуёт:** `core.api("/anime/<id>/videos")` → `site_me.kodik_groups(items)`.

### 4. `kodik_season_player.html` — 19 375 Б — **содержит реальные токены**

- **URL:** `https://kodikplayer.com/season/111506/8019bbcdcf0da3cb00d4ab0e90ad6eb5/720p?translations=false&only_episode=true&only_season=true&episode=1`
- **Referer:** `https://old.yummyani.me/catalog/item/mag-vody` (iframe взят из `iframe_url` в п.3, серия 1)
- **Содержит:** страницу плеера Kodik для `.me`-тайтла. Все 6 sig-переменных на месте
  (`var domain`, `var d_sign`, `var pd`, `var pd_sign`, `var ref`, `var ref_sign` —
  последние три это подписи с временем жизни, реальные и протухнут), плюс
  `.type = 'seria'`, `.hash = '8019bbcdcf0da3cb00d4ab0e90ad6eb5'`, `.id = '1478013'`.
- **Жуёт:** `tv_adapter.parse_sig()` (сиг-поля) и регекспы `\.type / \.hash / \.id`
  из `core.kodik_m3u8()` (собирает payload для `/ftor`).

### 5. `kodik_serial_player.html` — 36 590 Б

- **URL:** `https://kodikplayer.com/serial/68923/ac726260717824f47286e55e43fc62d6/720p`
- **Как получено:** `tv_adapter.SerialSession` — `prime(iframe)` ставит kodik-cookie,
  затем `page(media_id, media_hash)` с Referer предыдущей страницы.
  Это ровно та цепочка, что даёт не дефолтный, а запрошенный перевод.
- **Содержит:** `.serial-translations-box` со **10 переводами** в `<option>`:
  AniJoy (tid 2403), **AniStar (tid 910 — `selected="selected"`, 11 серий)**,
  Beloved, Dream Cast, SHIZA Project, ТО Дубляжная, AniJoy.Subtitles (ep=11),
  KALGAZM (ep=5), SubVost.Subtitles (ep=2), KoeKak (ep=1).
  Атрибуты каждого: `data-media-id`, `data-media-hash`, `data-media-type`,
  `data-translation-type`, `data-episode-count`, `data-id`, `data-title`.
  Плюс `.series-options` с сезонами: **сезон 0 — 4 серии, сезон 1 — 11 серий**,
  выбран сезон 1; и полный набор sig-переменных страницы.
- **Жуёт:** `tv_adapter.parse_translations()`, `tv_adapter.parse_seasons()`,
  `tv_adapter.parse_sig()`, `tv_adapter.selected_translation_id()`.

### 6. `kodik_ftor_response.json` — 15 211 Б — **содержит реальные токены**

- **URL:** `POST https://kodikplayer.com/ftor`
- **Тело:** payload из п.4 — `d`, `d_sign`, `pd`, `pd_sign`, `ref`, `ref_sign`,
  `bad_user=false`, `cdn_is_working=true`, `type=seria`,
  `hash=8019bbcdcf0da3cb00d4ab0e90ad6eb5`, `id=1478013`
- **Referer:** URL Kodik-страницы из п.4
- **Содержит:** сырое тело ответа. Ключи: `links`, `default`, `domain`, `ip`,
  `advert_script`, `vast`, `reserve_vast`.
  `links` — **ровно `{360, 480, 720}`** (по одной ссылке на качество),
  `src` в каждой — kodik-закодированная строка (base64 + сдвиг букв),
  которую надо пропустить через `core.kodik_decode()`.
- **Жуёт:** `core.kodik_m3u8()` / `core.tv_m3u8()` — выбор качества из `links`
  и `kodik_decode(links["720"][0]["src"])`.

### 7. `tv_card.html` — 49 538 Б

- **URL:** `https://yummyanime.tv/8383-nukitasi-d1.html`
- **Получен:** `tv_adapter.TvOpener().get()` (Chrome UA + cookie-jar), как в реальном handshake
- **Содержит:** HTML карточки. `data-player-anime-id="8383"` и блоки плееров:
  `data-player-slot="0"` / `data-player-title="Плеер Все озвучки"`,
  `data-player-slot="1"` / `data-player-title="Плеер Kodik"`.
  Также `og:title`, `h1`, описание, список серий.
- **Жуёт:** `tv_adapter.tv_card_id()`, `tv_adapter._card_slots()`,
  `tv_adapter.tv_card_meta()`.

### 8. `tv_player_relay.js` — 1 522 Б

- **URL:** `GET https://yummyanime.tv/theme/js/player-relay.js`
- **Получен:** `TvOpener().get()`, `Referer` = карточка
- **Содержит:** клиентскую логику `.tv`-relay — сборка URL
  `/engine/ajax/controller.php?mod=player&id=&slot=` из
  `data-player-*` атрибутов карточки. Эталон того, какой контракт ждёт сервер.
- **Жуёт:** справочный/регрессионный (контракт запроса), прямого парсера в
  `tv_adapter.py` нет.

### 9. `tv_xfp.html` — 6 289 Б

- **URL:** `GET https://yummyanime.tv/engine/ajax/controller.php?mod=xfp`
- **Получен:** `TvOpener().get()`, `Referer` = карточка
- **Содержит:** relay-болванку — ставит куки/сессию, которые нужны следующему
  шагу. Как отдельный JSON не разбирается, но должен быть в цепочке handshake,
  иначе слоты отдадут не то.
- **Жуёт:** используется только как `Referer` для п.10 в `tv_slots()`.

### 10. `tv_player_slot0.json` — 243 Б, `tv_player_slot1.json` — 154 Б — **содержат реальные токены**

- **URL:** `GET https://yummyanime.tv/engine/ajax/controller.php?mod=player&id=8383&slot=0`
  и `...&slot=1`
- **Получен:** `TvOpener().get()` с `Referer` = xfp (п.9) и
  `X-Requested-With: XMLHttpRequest`, `Accept: application/json`
- **Содержит:** сырые тела. Одинаковая форма `{status, data}`:
  - `slot0` — `status=true`, `data.kind="iframe"`, `name="Плеер Все озвучки"`,
    `src` = Alloha-iframe `https://ani5.top/2/embed/agp?a=8383&x=...&s=504cffee...`
  - `slot1` — `status=true`, `data.kind="iframe"`, `name="Kodik"`,
    `src` = `https://kodikplayer.com/serial/68923/ac726260717824f47286e55e43fc62d6/720p`
- **Жуёт:** `tv_adapter.tv_slots()` (в т.ч. `tv_kodik_iframes()`, который
  отбирает `kind == "iframe"` + `kodikplayer.com` в `src`).
  Второй src — вход в фикстуру п.5.

## Что не удалось

Ничего: все 11 файлов получены. Дополнительно (сверх списка, для сверки
второго образца) проверено, что `https://old.yummyani.me/catalog/item/dalekij-paladin-2026-03-31`
отдаёт `page_id=204`, но сам HTML второго тайтла не сохранялся — он не был в задании.

## Замечания для тестов

- Слоты `.tv` нумеруются с 0; слоты 2+ отдают 404 и в `tv_slots()` пропускаются —
  на фикстурах нет примера такого ответа.
- `tv_card.html` содержит 19 строк с CRLF — так отдал сайт, файл оставлен
  байт-в-байт.
- Фикстуры 4/6/10 содержат подписи с ограниченным сроком жизни и одноразовые
  токены. Для оффлайн-парсинга это нормально, для живых запросов — нет:
  реальный `POST /ftor` на живой странице надо повторять заново.
