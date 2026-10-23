#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YummyDownloader — качалка аниме с YummyAnime (old.yummyani.me, yummyanime.tv)
без проигрывания плеера.

Это тонкий слой поверх ядра и адаптеров сайтов:

  core.py        скачивание/пулы/склейка/resume/ffmpeg и разбор Kodik — сайтов не знает
  site_adapter.py интерфейс адаптера (EpisodeRef/TitleInfo/SiteAdapter)
  site_me.py     old.yummyani.me: страница тайтла + /api/anime/<id>/videos
  site_tv.py     yummyanime.tv: handshake (tv_adapter) + kodik serial-серии
  seg_dl.py      сегментное скачивание HLS (не трогаем)
  tv_adapter.py  низкоуровневый вход на yummyanime.tv (не трогаем)

Как это работает (никаких спойлеров-превью):
  1. Адаптер по домену отдаёт TitleInfo: название тайтла, группы озвучек/сабов
     и по серии — EpisodeRef (обычный iframe Kodik либо kodik-serial).
  2. Для выбранной группы ядро разбирает плеер Kodik (POST /ftor + декод ссылки)
     и получает m3u8 нужного качества.
  3. ffmpeg/seg_dl сами качают HLS-поток и складывают в .mp4. Раскладываем по папкам.

Все имена, которыми пользуется yummy_gui.py, остались здесь как обёртки/реэкспорты.

Запуск:
  py yummy_dl.py "https://old.yummyani.me/catalog/item/...."     — скачать один тайтл
  py yummy_dl.py --backend httpx "https://..."                   — HTTP-бэкенд сегментов
  py yummy_dl.py --backend urllib "https://..."                  — старая цепочка urllib->curl
  (по умолчанию auto: httpx, если пакет установлен, иначе urllib->curl)

Настройки — в config.json рядом со скриптом ("http_backend": "auto").
"""

import os
import sys

# Консоль cmd на русской Windows — cp866; не падаем на редких символах вывода.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

# --------------------------------------------------------------------------- core
import core
from core import (HERE, DEFAULT_CONFIG, CFG, SITE, UA, CTX, FFMPEG,
                  load_config,
                  http_get, http_post, api,
                  sanitize, parse_ep_range,
                  SUB_PREFIX, SUB_SUFFIX, is_sub, choose_dubbing,
                  ROT_TABLES, kodik_decode, kodik_m3u8, tv_m3u8, kodik_resolve,
                  download_hls, hls_file_ok,
                  select_group, pool_sizes, plan_todo, run_episodes, run_title,
                  episode_path)

# ------------------------------------------------------------------- site-адаптеры
from site_adapter import (SiteAdapter, TitleInfo, EpisodeRef, KIND_IFRAME,
                          KIND_SERIAL, ME_HOST, TV_HOST, get_adapter, is_tv_url)
import site_me
import site_tv
from site_tv import tv_enumerate


# --------------------------------------------------------------------------- site
def resolve_page_id(url):
    """Из ссылки на тайтл вытащить числовой page_id (old.yummyani.me)."""
    return site_me.resolve_page_id(url)


def kodik_groups(items):
    """Сгруппировать серии по озвучке (только плеер Kodik) — .me."""
    return site_me.kodik_groups(items)


def title_info(url_or_id, cfg=None):
    """Ссылка/id -> TitleInfo через адаптер, выбранный по домену."""
    return get_adapter(url_or_id, cfg or CFG).title_info(url_or_id)


# --------------------------------------------------------------------------- .me
def _download_episode(num, iframe, quality, out_dir, seg_workers):
    """Одна серия: m3u8 -> download_hls. Возвращает (pick_quality, size_bytes).
    Исключение одной серии не трогает остальные (ловит вызывающий код)."""
    return core._download_episode_ref(num, iframe, quality, out_dir, seg_workers)


def grab_anime(anime_id, cfg, franchise_dir=None, dub_override=None, ep_filter=None):
    ad = get_adapter(anime_id, cfg)
    ti = ad.title_info(anime_id)
    info = ti.meta.get("api_info") or {}
    title = ti.title
    run_title(title, ti.groups, cfg, ad, ti,
              dub_override=dub_override, ep_filter=ep_filter,
              franchise_dir=franchise_dir,
              empty_msg=f"[!] {title}: озвучек в плеере Kodik не нашлось (есть только Alloha/CVH) - пропуск",
              nodub_msg=f"[!] {title}: подходящих озвучек в плеере Kodik не нашлось (субтитры скрыты) - пропуск")
    return info


def ask_menu(info, items, cfg):
    """Интерактивный выбор: озвучка -> качество -> серии -> все сезоны. Возвращает dict override."""
    title = info.get("title", "?")
    groups = kodik_groups(items)
    print(f"\n========== {title} ==========")
    if not groups:
        print("В плеере Kodik озвучек нет (только Alloha/CVH) — этот тайтл не качается.")
        return None

    # --- 1. озвучка / субтитры ---
    allow_subs = bool(cfg.get("allow_subtitles", True))
    if any(is_sub(n) for n in groups):
        if allow_subs:
            a = input("В списке есть субтитры. Скрыть их? (y/N): ").strip().lower()
            allow_subs = a != "y"
        else:
            a = input("Субтитры сейчас скрыты. Показать их? (y/N): ").strip().lower()
            allow_subs = a == "y"
    names = sorted(groups, key=lambda n: -len(groups[n]))
    if not allow_subs:
        names = [n for n in names if not is_sub(n)]
    if not names:
        print("В плеере Kodik озвучек нет (только субтитры, а они скрыты) — этот тайтл не качается.")
        return None
    default_dub = choose_dubbing(groups, cfg.get("preferred_dubbings") or [],
                                 allow_subs=allow_subs,
                                 sub_prefs=cfg.get("preferred_subs"))
    if default_dub not in names:
        default_dub = names[0]
    print("\nДоступные озвучки и субтитры:")
    for i, n in enumerate(names, 1):
        mark = "  <- по умолчанию" if n == default_dub else ""
        print(f"  {i}. {n}  ({len(groups[n])} сер.){mark}")
    sel = input("Выбери озвучку (номер, Enter = по умолчанию): ").strip()
    dub = names[int(sel) - 1] if sel.isdigit() and 1 <= int(sel) <= len(names) else default_dub
    eps_avail = sorted(groups[dub])
    if not eps_avail:
        print("[!] в этой озвучке нет ни одной серии — выбери другую.")
        return None

    # --- 2. качество ---
    print("\nКачество:  1) макс (обычно 720)   2) 480   3) 360")
    qmap = {"1": "max", "2": "480", "3": "360", "": cfg["quality"]}
    q = qmap.get(input(f"Выбери качество (Enter = {cfg['quality']}): ").strip(), cfg["quality"])

    # --- 3. серии ---
    print(f"\nСерии в этой озвучке: {eps_avail[0]}–{eps_avail[-1]} (всего {len(eps_avail)})")
    rng = input("Какие качать? (Enter = все, или напр. 5-12 / 3,7,9): ").strip()
    keep = parse_ep_range(rng, eps_avail)
    if not keep:
        print("[!] таких серий в этой озвучке нет — отменено.")
        return None

    # --- 4. все сезоны франшизы ---
    vo = info.get("viewing_order") or []
    other = [v for v in vo if v.get("anime_id") and v["anime_id"] != info.get("anime_id")]
    allseasons = False
    if other:
        a = input(f"\nКачать всю франшизу (ещё {len(other)} тайтл(ов))? (y/N): ").strip().lower()
        allseasons = a == "y"

    kind = "субтитры" if is_sub(dub) else "озвучка"
    print(f"\n>> Итог: {kind} «{dub}», качество {q}, серий {len(keep)}"
          + (", вся франшиза" if allseasons else ""))
    if input("Начать? (Y/n): ").strip().lower() == "n":
        return None
    return {"dub": dub, "quality": q, "episodes": keep, "all_seasons": allseasons,
            "allow_subs": allow_subs}


# --------------------------------------------------------------------------- .tv
# Ветка yummyanime.tv поверх того же Kodik-пайплайна (kodik_resolve/kodik_decode,
# download_hls/seg_dl, ThreadPool серий). Alloha/CVH не тронуты.

def _download_tv_episode(num, src, quality, out_dir, seg_workers):
    """Одна .tv-серия: src ('tv',sig,id,hash,ref) или iframe-строка (single)."""
    return core._download_episode_ref(num, src, quality, out_dir, seg_workers)


def tv_grab_anime(card_url, cfg, dub_override=None, ep_filter=None, enum=None):
    """Скачать сезон .tv-карточки. Структура пула — общая (core.run_title)."""
    ad = site_tv.TvSite(cfg)
    enum = enum or tv_enumerate(card_url)
    ti = site_tv.enum_to_title_info(enum)
    title = ti.title
    dubs = enum.get("dubs") or {}
    season_extra = {n: ("" if d.get("single") else f"   сезон: {d.get('season')}")
                    for n, d in dubs.items()}
    run_title(title, ti.groups, cfg, ad, ti,
              dub_override=dub_override, ep_filter=ep_filter,
              header_extra=lambda d: f"    плеер: {enum.get('slot')}{season_extra.get(d, '')}",
              empty_msg=f"[!] {title}: озвучек в плеере Kodik не нашлось - пропуск",
              nodub_msg=f"[!] {title}: подходящих озвучек в плеере Kodik не нашлось (субтитры скрыты) - пропуск",
              thread_prefix="tvep")
    return enum


def tv_ask_menu(enum, cfg):
    """Интерактивный выбор для .tv: озвучка -> качество -> серии."""
    dubs = enum.get("dubs") or {}
    if not dubs:
        print("В плеере Kodik озвучек нет — этот тайтл не качается.")
        return None
    groups = {n: ({1: None} if d.get("single") else (d.get("eps") or {}))
              for n, d in dubs.items()}
    allow_subs = bool(cfg.get("allow_subtitles", True))
    if any(is_sub(n) for n in groups):
        if allow_subs:
            a = input("В списке есть субтитры. Скрыть их? (y/N): ").strip().lower()
            allow_subs = a != "y"
        else:
            a = input("Субтитры сейчас скрыты. Показать их? (y/N): ").strip().lower()
            allow_subs = a == "y"
    names = sorted(groups, key=lambda n: -len(groups[n]))
    if not allow_subs:
        names = [n for n in names if not is_sub(n)]
    if not names:
        print("В плеере Kodik озвучек нет (только субтитры, а они скрыты) — этот тайтл не качается.")
        return None
    default_dub = choose_dubbing(groups, cfg.get("preferred_dubbings") or [],
                                 allow_subs=allow_subs,
                                 sub_prefs=cfg.get("preferred_subs"))
    if default_dub not in names:
        default_dub = names[0]
    print(f"\n========== {enum.get('title', '?')} (.tv, плеер {enum.get('slot')}) ==========")
    print("\nДоступные озвучки и субтитры:")
    for i, n in enumerate(names, 1):
        mark = "  <- по умолчанию" if n == default_dub else ""
        print(f"  {i}. {n}  ({len(groups[n])} сер.){mark}")
    sel = input("Выбери озвучку (номер, Enter = по умолчанию): ").strip()
    dub = names[int(sel) - 1] if sel.isdigit() and 1 <= int(sel) <= len(names) else default_dub
    eps_avail = sorted(groups[dub])
    if not eps_avail:
        print("[!] в этой озвучке нет ни одной серии — выбери другую.")
        return None
    print("\nКачество:  1) макс (обычно 720)   2) 480   3) 360")
    qmap = {"1": "max", "2": "480", "3": "360", "": cfg["quality"]}
    q = qmap.get(input(f"Выбери качество (Enter = {cfg['quality']}): ").strip(), cfg["quality"])
    print(f"\nСерии в этой озвучке: {eps_avail[0]}–{eps_avail[-1]} (всего {len(eps_avail)})")
    rng = input("Какие качать? (Enter = все, или напр. 5-12 / 3,7,9): ").strip()
    keep = parse_ep_range(rng, eps_avail)
    if not keep:
        print("[!] таких серий в этой озвучке нет — отменено.")
        return None
    kind = "субтитры" if is_sub(dub) else "озвучка"
    print(f"\n>> Итог: {kind} «{dub}», качество {q}, серий {len(keep)}")
    if input("Начать? (Y/n): ").strip().lower() == "n":
        return None
    return {"dub": dub, "quality": q, "episodes": keep, "allow_subs": allow_subs}


def tv_grab(card_url, cfg=None, interactive=False):
    cfg = dict(cfg or CFG)
    enum = tv_enumerate(card_url)
    dub_override = None
    ep_filter = None
    if interactive:
        choice = tv_ask_menu(enum, cfg)
        if not choice:
            print("Отменено.")
            return
        cfg["quality"] = choice["quality"]
        cfg["allow_subtitles"] = choice.get("allow_subs", cfg.get("allow_subtitles", True))
        dub_override = choice["dub"]
        keep = choice["episodes"]
        ep_filter = (lambda n: n in keep)
    tv_grab_anime(card_url, cfg, dub_override=dub_override,
                  ep_filter=ep_filter, enum=enum)
    print("\nГотово.")


def grab(url_or_id, cfg=None, interactive=False):
    cfg = dict(cfg or CFG)
    if is_tv_url(url_or_id):
        tv_grab(url_or_id, cfg, interactive=interactive)
        return
    if isinstance(url_or_id, str) and url_or_id.startswith("http"):
        page_id = resolve_page_id(url_or_id)
    else:
        try:
            page_id = int(str(url_or_id).strip())
        except (TypeError, ValueError):
            print("[!] не понял ссылку/ID: %r" % (url_or_id,))
            return

    dub_override = None
    ep_filter = None
    if interactive:
        info0 = api(f"/anime/{page_id}")
        items0 = api(f"/anime/{page_id}/videos")
        choice = ask_menu(info0, items0, cfg)
        if not choice:
            print("Отменено.")
            return
        cfg["quality"] = choice["quality"]
        cfg["all_seasons"] = choice["all_seasons"]
        cfg["allow_subtitles"] = choice.get("allow_subs", cfg.get("allow_subtitles", True))
        dub_override = choice["dub"]
        keep = choice["episodes"]
        ep_filter = (lambda n: n in keep)

    info = grab_anime(page_id, cfg, dub_override=dub_override, ep_filter=ep_filter)
    if cfg.get("all_seasons") and info:
        vo = info.get("viewing_order") or []
        seasons = [v for v in vo if v.get("anime_id") and v["anime_id"] != page_id]
        if seasons:
            franchise = os.path.join(cfg["output_dir"], sanitize(vo[0].get("title") or info.get("title")))
            print(f"\n>>> Франшиза: ещё {len(seasons)} связанн(ых) тайтл(ов)")
            # остальные сезоны качаем целиком в выбранной озвучке (если она там есть)
            for v in seasons:
                try:
                    grab_anime(v["anime_id"], cfg, franchise_dir=franchise, dub_override=dub_override)
                except Exception as e:
                    print(f"[!] сезон {v.get('title')}: {e}")
    print("\nГотово.")


# --------------------------------------------------------------------------- main
def _pop_int_flag(args, *names):
    """Вытащить из argv значение флага (--workers 4). Возвращает (value|None, остаток)."""
    out, val, i = [], None, 0
    while i < len(args):
        if args[i] in names and i + 1 < len(args) and args[i + 1].isdigit():
            val = int(args[i + 1])
            i += 2
            continue
        out.append(args[i])
        i += 1
    return val, out


def _pop_str_flag(args, *names):
    """Вытащить из argv строковое значение флага (--backend httpx).
    Возвращает (value|None, остаток). Значение не начинается с '-'."""
    out, val, i = [], None, 0
    while i < len(args):
        if (args[i] in names and i + 1 < len(args)
                and not args[i + 1].startswith("-")):
            val = args[i + 1]
            i += 2
            continue
        out.append(args[i])
        i += 1
    return val, out


if __name__ == "__main__":
    os.makedirs(CFG["output_dir"], exist_ok=True)
    args = [a for a in sys.argv[1:] if a]
    workers, args = _pop_int_flag(args, "--workers")
    segs, args = _pop_int_flag(args, "--segs", "--seg-workers")
    backend, args = _pop_str_flag(args, "--backend")
    run_cfg = dict(CFG)
    if "--no-subs" in args:
        run_cfg["allow_subtitles"] = False
        args = [a for a in args if a != "--no-subs"]
    if "--subs" in args:
        run_cfg["allow_subtitles"] = True
        args = [a for a in args if a != "--subs"]
    if workers:
        run_cfg["workers"] = max(1, min(workers, 8))
    if segs is not None:
        # 0 = форсировать прямой ffmpeg без сегментов (диагностика микрофризов)
        run_cfg["seg_workers"] = max(0, min(segs, 32))
    if backend:
        # выбор HTTP-бэкенда для сегментов (auto|httpx|urllib); httpx опционален
        run_cfg["http_backend"] = backend
        try:
            import seg_dl
            print(f"HTTP-бэкенд сегментов: {seg_dl.set_backend(backend)}")
        except Exception as e:
            print(f"[!] {e}")
            sys.exit(2)
    if not args:
        url = input("Вставь ссылку на тайтл: ").strip()
        args = [url] if url else []
    if not args:
        print("Ссылка не указана.")
        sys.exit(1)
    # запуск с ссылкой -> интерактивное меню (озвучка/качество/серии/франшиза)
    grab(args[0], run_cfg, interactive=True)