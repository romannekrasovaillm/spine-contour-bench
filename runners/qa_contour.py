#!/usr/bin/env python3
"""qa_contour.py — сводка целостности прогона по ячейкам.

Отвечает на вопросы, которые надо задать ДО чтения результатов:

  * откуда взялся артефакт — файл в рабочем каталоге или фолбэк на stdout
    (`meta.json:deliverable_source`)? Фолбэк — не брак, но это характеристика
    харнесса, и её надо видеть;
  * сдана ли стадия 1 и стадия 2 (meta.json / meta2.json, ошибки в них);
  * создал ли преемник RESUME.md (викторина вообще состоялась?);
  * сколько ячеек уложилось в бюджет времени;
  * вычислился ли по ячейке весь набор метрик (гейт, дефекты, handoff).

Печатает строку `PVBENCH_CELLS=«…»` со списком ячеек, которые надо догнать.

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/qa_contour.py [--glob '*']
Только stdlib.
"""
import argparse
import fnmatch
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

CELLS = pvlib.CELLS_DIR
MIN_BYTES = 2000


def read_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def check(cell):
    name = cell.name
    m1 = read_json(cell / "meta.json")
    m2 = read_json(cell / "meta2.json")
    work = cell / "work"
    ans = work / "answer.md"
    rec = {"cell": name, "blocks": []}
    if not m1:
        rec["blocks"].append("нет meta.json — стадия 1 не запускалась")
    if m1.get("error"):
        rec["blocks"].append(f"стадия 1: {str(m1['error'])[:80]}")
    if ans.is_file() and ans.stat().st_size < MIN_BYTES:
        rec["blocks"].append(f"артефакт подозрительно мал ({ans.stat().st_size} б)")
    if not ans.is_file():
        rec["blocks"].append("нет work/answer.md")
    rec["source1"] = m1.get("deliverable_source")
    rec["secs1"] = m1.get("secs")
    if (cell / "injection.json").is_file():
        rec["stage2_expected"] = True
        if not m2:
            rec["blocks"].append("стадия 2 не запускалась (нет meta2.json)")
        elif m2.get("error"):
            rec["blocks"].append(f"стадия 2: {str(m2['error'])[:80]}")
        rec["resume_md"] = bool(m2.get("resume_md_created"))
        rec["source2"] = m2.get("deliverable_source")
        rec["secs2"] = m2.get("secs")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="*")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    cells = [p for p in sorted(CELLS.iterdir())
             if p.is_dir() and fnmatch.fnmatch(p.name, a.glob)]
    recs, bad = [], []
    print(f"{'ячейка':44s} {'s1':>6s} {'s2':>6s} {'источник':>9s} {'RESUME':>7s} пометки")
    for c in cells:
        r = check(c)
        recs.append(r)
        if r["blocks"]:
            bad.append(r["cell"])
        print(f"{r['cell']:44s} {str(r.get('secs1') or '—'):>6s} "
              f"{str(r.get('secs2') or '—'):>6s} "
              f"{str(r.get('source1') or '—'):>9s} "
              f"{('да' if r.get('resume_md') else '—' if r.get('stage2_expected') else ''):>7s} "
              f"{'; '.join(r['blocks'])[:70]}")
    print(f"\nячеек: {len(recs)}; с пометками: {len(bad)}")
    if bad:
        print('PVBENCH_CELLS="' + " ".join(bad) + '"')
    if a.json:
        Path(a.json).write_text(json.dumps(recs, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
