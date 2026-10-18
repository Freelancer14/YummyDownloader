#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_tests — запуск всех тестов парсеров без сети.

    python tests/run_tests.py            # все тесты, подробный вывод
    python tests/run_tests.py -q         # тихий режим
    python tests/run_tests.py test_parsers.MeTitlePageTests

Код возврата: 0 — все зелёные, 1 — есть падения/ошибки.
Никаких внешних зависимостей: только stdlib unittest.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Корень проекта в sys.path — чтобы `import core, site_me, ...` работали
# без установки пакета; tests/ — чтобы discover видел тестовые модули.
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)


def main(argv):
    verbosity = 1 if "-q" in argv or "--quiet" in argv else 2
    names = [a for a in argv if not a.startswith("-")]

    loader = unittest.TestLoader()
    if names:
        suite = loader.loadTestsFromNames(names)
    else:
        suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)

    print()
    total = result.testsRun
    print("итого: %d тестов, падений: %d, ошибок: %d, пропущено: %d"
          % (total, len(result.failures), len(result.errors),
             len(result.skipped)))
    print("результат: %s" % ("OK" if result.wasSuccessful() else "FAILED"))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
