#!/usr/bin/env python3
"""audit_isolation.py — проверка, что рука не читала дизайн бенчмарка.

ЗАЧЕМ. Ячейки лежат в дереве каталогов бенчмарка, поэтому из рабочего
каталога агент доходит относительным путём до файлов дизайна: корпуса
инъекций (`contour/<T>/DEFECTS.yaml`), ключа викторины
(`handoff_quiz.yaml`), кода счёта (`runners/contour_score.py`), правил
судейства (`tasks/<T>/RUBRICS.md`) и пререгистрации. Рука, прочитавшая их,
знает ответы, и измерение становится недействительным. Это не гипотеза:
на прогоне 15–16.09.2026 так и произошло — 73 ячейки нового бенчмарка и
92 ячейки первого (`DEVIATIONS.md` D13).

Перенос прогонов за пределы репозитория закрывает относительный доступ,
но не доказывает, что никто не пошёл искать по абсолютным путям. Поэтому
нужна ПРОВЕРКА, а не надежда: этот инструмент читает журналы сессий и
помечает ячейки, где был доступ за пределы рабочего каталога.

Что считается нарушением:
  * любой путь в вызове инструмента, ведущий ВЫШЕ рабочего каталога ячейки;
  * переход вверх по дереву (`..`) в команде;
  * упоминание файлов дизайна (DEFECTS.yaml, handoff_quiz.yaml,
    PREREGISTRATION.md, contour_score.py, contour_prep.py, inject.py,
    audit_isolation.py, prereg.lock.json, RUBRICS.md задачи).

Покрытие журналов по харнессам (честно, потому что полнота разная):
  claude   ~/.claude/projects/<slug>/*.jsonl — разбираются вызовы целиком;
  theseus  <cell>/work/.theseus/*.json(l)    — поиск по тексту;
  arch-be  ~/.arch-harness/sessions/*.jsonl  — сопоставление по cwd ячейки;
  kimi     ~/.kimi-code/sessions/**          — сопоставление по cwd ячейки.
Где журнала нет — ячейка помечается `no_log`, а не «чисто»: отсутствие
данных не есть отсутствие нарушений.

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/audit_isolation.py [--json отчёт]
Код возврата: 0 — нарушений нет, 1 — есть. Только stdlib.
"""
import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

CELLS = pvlib.CELLS_DIR
HOME = Path.home()
CLAUDE_PROJ = HOME / ".claude" / "projects"
ARCH_SESS = HOME / ".arch-harness" / "sessions"
KIMI_SESS = HOME / ".kimi-code" / "sessions"

# Имена файлов дизайна. Голое `RUBRICS.md` в список НЕ входит: оно есть
# в промпте каждой руки (frontmatter TASK.md, related_files) и давало
# совпадение на всех ячейках без разбора. Правила судейства ловятся
# отдельным шаблоном — по пути `tasks/<TASK>/RUBRICS.md`.
DESIGN = ("DEFECTS.yaml", "handoff_quiz.yaml", "PREREGISTRATION.md",
          "prereg.lock.json", "contour_score.py", "contour_prep.py",
          "inject.py", "audit_isolation.py", "AEF-1-CHECKLIST.md",
          "COI-POLICY.md", "gate_extra.py", "stage2_resume.py",
          "DEVIATIONS.md", "prepare_cells.py", "run_matrix.py")
TASK_RUBRIC = re.compile(r"tasks/[A-Z]{3}-[A-Z]+-\d+/RUBRICS\.md")
UP = re.compile(r"(?<![\w.])\.\./")          # переход выше по дереву
PATHISH = re.compile(r'"(file_path|path|notebook_path|command|pattern)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def scan_text(text):
    """Признаки доступа за пределы рабочего каталога (консервативно)."""
    hits = set()
    for m in PATHISH.finditer(text):
        val = m.group(2).encode().decode("unicode_escape", "replace")
        if UP.search(val):
            hits.add("..")
        for d in DESIGN:
            if d in val:
                hits.add(d)
    if UP.search(text):
        hits.add("..")
    for d in DESIGN:
        if d in text:
            hits.add(d)
    if TASK_RUBRIC.search(text):
        hits.add("RUBRICS.md-задачи")
    return hits


def cell_hits(cell):
    """{источник: множество признаков} по всем найденным журналам ячейки."""
    work = (cell / "work").resolve()
    found = {}

    # 1. claude: журнал по slug рабочего каталога
    d = CLAUDE_PROJ / slug(work)
    if d.is_dir():
        h = set()
        for f in d.glob("*.jsonl"):
            h |= scan_text(f.read_text(encoding="utf-8", errors="replace"))
        found["claude"] = h

    # 2. theseus: журналы внутри ячейки
    for f in (work / ".theseus").glob("*") if (work / ".theseus").is_dir() else []:
        if f.is_file() and f.suffix in (".json", ".jsonl"):
            found.setdefault("theseus", set())
            found["theseus"] |= scan_text(
                f.read_text(encoding="utf-8", errors="replace"))

    # 3. arch-be / kimi: общие каталоги сессий, сопоставляем по cwd ячейки
    for name, base in (("arch-be", ARCH_SESS), ("kimi", KIMI_SESS)):
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.suffix not in (".json", ".jsonl"):
                continue
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if str(work) not in txt:
                continue
            found.setdefault(name, set())
            found[name] |= scan_text(txt)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    recs, bad = [], []
    cov = {}
    for c in sorted(CELLS.iterdir()):
        if not c.is_dir():
            continue
        hits = cell_hits(c)
        cov_key = "+".join(sorted(hits)) or "нет журнала"
        cov[cov_key] = cov.get(cov_key, 0) + 1
        rec = {"cell": c.name, "sources": {k: sorted(v) for k, v in hits.items()},
               "violations": sorted({x for v in hits.values() for x in v})}
        recs.append(rec)
        if rec["violations"]:
            bad.append(rec)
            print(f"  !! {c.name:44s} {', '.join(rec['violations'])[:70]}")
    print(f"\nпроверено ячеек: {len(recs)}; с признаками доступа к дизайну: {len(bad)}")
    print("покрытие журналов: " + "; ".join(f"{k} — {v}" for k, v in
                                            sorted(cov.items(), key=lambda kv: -kv[1])))
    if bad:
        print('PVBENCH_CELLS="' + " ".join(r["cell"] for r in bad) + '"')
    if a.json:
        Path(a.json).write_text(json.dumps(recs, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
