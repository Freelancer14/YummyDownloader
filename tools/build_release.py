#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_release — релизный ZIP только с тем, что нужно для запуска.

Зачем: GitHub «Code -> Download ZIP» отдаёт весь репозиторий, включая tests/
(579 КБ фикстур) и служебные файлы. Пользователю это не нужно, поэтому
раскладываем рядом артефакт с белым списком: программа + два .cmd + зависимости.

    py -3.9 tools/build_release.py [--out DIR]

Результат: dist/YummyDownloader-<версия>.zip, файлы лежат в корне архива
(Windows сам создаст папку при распаковке). Скрипт намеренно падает, если
какого-то файла нет: молча собрать неполный релиз хуже, чем не собрать вовсе.
"""

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Всё, без чего программа не запустится или не установится.
INCLUDE = [
    "yummy_gui.py",        # окно
    "yummy_dl.py",         # CLI + публичный API
    "core.py",             # ядро
    "seg_dl.py",           # сегментная качка
    "site_adapter.py",     # контракт сайтов
    "site_me.py",          # old.yummyani.me
    "site_tv.py",          # yummyanime.tv
    "tv_adapter.py",       # DLE-релей .tv
    "Start_GUI.cmd",       # запуск окна
    "Setup_Python.cmd",    # установка Python + зависимостей
    "requirements.txt",
    "config.example.json",
    "VERSION",
    "README.md",
    "LICENSE",
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Собрать релизный ZIP")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"),
                    help="куда положить архив (по умолчанию dist/)")
    args = ap.parse_args(argv)

    vpath = os.path.join(ROOT, "VERSION")
    if not os.path.isfile(vpath):
        print("[X] нет VERSION")
        return 1
    with open(vpath, encoding="utf-8") as f:
        version = f.read().strip()
    if not version:
        print("[X] VERSION пуст")
        return 1

    missing = [n for n in INCLUDE if not os.path.isfile(os.path.join(ROOT, n))]
    if missing:
        print("[X] в проекте нет файлов: %s" % ", ".join(missing))
        return 1

    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "YummyDownloader-%s.zip" % version)
    if os.path.exists(out):
        os.remove(out)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in INCLUDE:
            # дата файла в архиве не важна, но фиксированный порядок — важен
            z.write(os.path.join(ROOT, name), arcname=name)

    size = os.path.getsize(out)
    print("собран: %s" % out)
    print("версия: %s, файлов: %d, размер: %.0f КБ" % (version, len(INCLUDE), size / 1024.0))
    for name in INCLUDE:
        print("  %-22s %7.1f КБ" % (name, os.path.getsize(os.path.join(ROOT, name)) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
