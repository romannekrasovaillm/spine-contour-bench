#!/usr/bin/env python3
"""readme_results.py — вставляет результаты из отчёта в README.

ЗАЧЕМ. README и `results/summary.md` не должны разъезжаться. Переносить
таблицы руками — значит однажды перенести не то (или перенести до того, как
отчёт пересчитан), и публичный документ начнёт говорить не то, что данные.
Здесь блок README собирается ИЗ отчёта: раздел между маркерами заменяется
целиком, вручную ничего не правится.

ЧТО ПОДСТАВЛЯЕТСЯ. Не весь отчёт: в README идут сводные таблицы и раздел
изоляции, а разбор отклонений остаётся в `DEVIATIONS.md` — README ссылается
на него, а не пересказывает.

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/readme_results.py [--dry-run]
Только stdlib.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

README = pvlib.BASE / "README.md"
START = "## Результаты"
END = "## Ограничения"

INTRO = """Результаты считаются один раз, после завершения прогона, и вставляются
сюда из `results/summary.md` (`runners/readme_results.py`) — вручную этот
раздел не правится, чтобы README не разошёлся с отчётом.

**Изоляция измерена, а не заявлена.** В отчёт входит статус каждой ячейки:
рука, читавшая ключ, из средних исключена. Числа ниже — по ячейкам, которые
прошли аудит (`results/audit.json`). Что именно нашлось и что с этим сделано —
`DEVIATIONS.md` D19.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run = pvlib.RUNS
    summary = run / "results" / "summary.md"
    if not summary.is_file():
        print(f"нет {summary} — сначала runners/contour_report.py")
        return 1
    text = summary.read_text(encoding="utf-8")
    # Заголовок отчёта в README не нужен: раздел уже назван.
    lines = [l for l in text.splitlines() if not l.startswith("# ")]
    body = "\n".join(lines).strip()

    rd = README.read_text(encoding="utf-8")
    i, j = rd.find(START), rd.find(END)
    if i < 0 or j < 0 or j < i:
        print(f"в README нет маркеров «{START}» / «{END}»")
        return 1
    block = (f"{START}\n\n{INTRO}\n"
             "---\n\n"
             f"{body}\n\n")
    new = rd[:i] + block + rd[j:]
    if a.dry_run:
        print(block[:1200])
        print(f"\n(сухой прогон: README не изменён; блок {len(block)} символов)")
        return 0
    README.write_text(new, encoding="utf-8")
    print(f"README обновлён: раздел «Результаты» — {len(block)} символов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
