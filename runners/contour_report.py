#!/usr/bin/env python3
"""contour_report.py — сводка результатов в markdown.

Собирает из `results/contour.jsonl` (пишет contour_score.py) и
`results/contour_effects.json` (пишет contour_effects.py) отчёт
`results/summary.md`: по рукам — средние метрики контура с числом ячеек,
таблица парных эффектов, честный блок целостности прогона (сколько ячеек
сдано, откуда взят артефакт, где сбой).

Числа не считаются заново: отчёт только раскладывает то, что уже посчитано,
чтобы README и отчёт не разъезжались.

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/contour_report.py [--out ПУТЬ]
Только stdlib.
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

RUNS = pvlib.RUNS
METRICS = [
    ("gate_pass", "гейт пройден целиком"),
    ("gate_green", "доля зелёных правил гейта"),
    ("trace_cov", "требований с адресным следом"),
    ("caught", "поймано внесённых дефектов"),
    ("leaked", "утекло дефектов"),
    ("alarm", "ложных срабатываний на контроле"),
    ("rules_restored", "структурных дефектов восстановлено"),
    ("packet", "пакет передачи полон"),
    ("coverage", "семейств содержания в передаче"),
    ("keyterm", "ключевых терминов решения в RESUME.md"),
]


def value(rec, name):
    d = rec.get("defects") or {}
    g = rec.get("gate_final") or {}
    h = rec.get("handoff") or {}
    # Ячейка без сданного финала не участвует ни в одной метрике: иначе
    # невыполненные ячейки попадали бы в средние нулями и занижали руки.
    if not g.get("present"):
        return None
    if name == "gate_pass":
        return None if not g.get("present") or "error" in g else int(bool(g.get("passed")))
    if name == "gate_green":
        if not g.get("present") or "error" in g or not g.get("rules_total"):
            return None
        return g["rules_green"] / g["rules_total"]
    if name == "trace_cov":
        c = ((g.get("contour") or {}).get("trace") or {}).get("detail") or {}
        return (c["covered"] / c["total"]) if c.get("total") else None
    if name in ("caught", "leaked"):
        return (d.get(name) or {}).get("value") if d.get("present") else None
    if name == "alarm":
        return (d.get("control_alarm") or {}).get("value") if d.get("present") else None
    if name == "rules_restored":
        rr = d.get("rules_restored") or {}
        return (rr["n"] / rr["d"]) if rr.get("d") else None
    if name == "packet":
        return int(bool(h.get("packet_complete")))
    if name == "coverage":
        return (h.get("coverage") / 5) if h.get("coverage") is not None else None
    if name == "keyterm":
        return (h.get("keyterm_recall") or {}).get("value")
    return None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return (round(statistics.mean(xs), 2), len(xs)) if xs else (None, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    src = RUNS / "results" / "contour.jsonl"
    if not src.is_file():
        print(f"нет {src} — сначала runners/contour_score.py")
        return 1
    recs = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    ef = RUNS / "results" / "contour_effects.json"
    effects = json.loads(ef.read_text(encoding="utf-8")) if ef.is_file() else None

    L = ["# Результаты: метрики контура", "",
         f"Ячеек в выгрузке: **{len(recs)}**; задач: "
         f"{len({r['task'] for r in recs})}; рук: {len({r['arm'] for r in recs})}.",
         "",
         "Метрики — средние по ячейкам руки (в скобках — число ячеек, по которым "
         "метрика определена; метрики дефектов и handoff считаются только на "
         "ячейках с запущенной стадией 2).", ""]

    arms = sorted({r["arm"] for r in recs})
    L += ["| Метрика | " + " | ".join(arms) + " |",
          "|---" * (len(arms) + 1) + "|"]
    for key, label in METRICS:
        cells = []
        for arm in arms:
            m, n = mean([value(r, key) for r in recs if r["arm"] == arm])
            cells.append(f"{m if m is not None else '—'}"
                         + (f" ({n})" if m is not None else ""))
        L.append(f"| {label} | " + " | ".join(cells) + " |")
    L.append("")

    if effects:
        L += ["## Парные эффекты", "", f"Метод: {effects.get('method','')}.", "",
              "`*` — доверительный интервал не пересекает ноль.", ""]
        for label, row in effects.get("effects", {}).items():
            L.append(f"**{label}**")
            L.append("")
            L.append("| Метрика | Δ | 95% CI | пар |")
            L.append("|---|---|---|---|")
            for key, v in row.items():
                if not v:
                    continue
                star = " *" if (v["ci_lo"] > 0 or v["ci_hi"] < 0) else ""
                L.append(f"| {v.get('metric', key)} | {v['diff']:+.3f} | "
                         f"[{v['ci_lo']:+.3f}; {v['ci_hi']:+.3f}]{star} | "
                         f"{v['n_pairs']} |")
            L.append("")

    # Целостность прогона
    L += ["## Целостность прогона", ""]
    stage2 = sum(1 for r in recs if r.get("stage2"))
    no_final = sum(1 for r in recs if not r.get("gate_final", {}).get("present"))
    L += [f"- ячеек со стадией 2 (преемник): **{stage2}**",
          f"- ячеек без финального артефакта: **{no_final}**",
          f"- ячеек с определённой метрикой пойманных дефектов: "
          f"**{sum(1 for r in recs if value(r,'caught') is not None)}**", ""]
    out = Path(a.out) if a.out else (RUNS / "results" / "summary.md")
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"записано: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
