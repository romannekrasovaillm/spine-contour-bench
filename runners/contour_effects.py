#!/usr/bin/env python3
"""contour_effects.py — парные эффекты контура с bootstrap-CI.

Читает `$PVBENCH_RUNS/results/contour.jsonl` (после `contour_score.py`) и
считает эффекты как ПАРНУЮ разность по ячейкам «задача × повтор»: из каждой
руки берутся только общие (task, rep) ячейки пары, d_i = a_i − b_i,
эффект = mean(d); 95% CI — bootstrap по d (10000 ресэмплов, seed=42, общий
ГСЧ на все эффекты в порядке спецификации — идиома paired_effects.py первого
бенчмарка).

Метрики контура берутся из записи `contour_score.py`; качество (mech_score,
judge total) — из `results.jsonl`, если он есть (его пишет отдельная цепочка
mech_score/judge, как в platformv-arch-bench).

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/contour_effects.py [--metric NAME]
Только stdlib.
"""
import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

RUNS = pvlib.RUNS
BOOT_N = 10000
SEED = 42

# (метрика, человеческое имя)
METRICS = {
    "caught": "поймано дефектов (доля из 6)",
    "leaked": "утекло дефектов (доля из 6)",
    "alarm": "ложных срабатываний (доля из 3)",
    "rules_restored": "восстановлено структурных дефектов (доля)",
    "gate_pass": "финал проходит гейт целиком",
    "gate_green": "доля зелёных правил гейта в финале",
    "trace_cov": "требований с адресным следом (доля)",
    "packet": "пакет передачи полон (4 файла)",
    "coverage": "семейств содержания в передаче (из 5)",
    "keyterm": "ключевых терминов решения в RESUME.md (доля)",
    "resume": "преемник написал RESUME.md",
}

# (метка, рука A, рука B, модель, метрика) — порядок = порядок отчёта
# Пары сравнений. Пятый элемент — статус: "prereg" означает, что пара
# зарегистрирована в PREREGISTRATION.md (H4 называет ровно две), остальные
# исследовательские. Различие показывается в отчёте: иначе послекритериальный
# выбор пар читался бы как подтверждение гипотезы, хотя это разведка.
EFFECTS = [
    ("формат: claude-spine − claude-plain (dsf)", "claude-spine", "claude-plain",
     "dsf", "prereg"),
    ("харнесс: spine-arch − claude-spine (dsf)", "spine-arch", "claude-spine",
     "dsf", "prereg"),
    ("формат: claude-spine − claude-arch (dsf)", "claude-spine", "claude-arch",
     "dsf", "explore"),
    ("формат: spine-arch − spine-min (dsf)", "spine-arch", "spine-min",
     "dsf", "explore"),
    ("формат: theseus-spine − theseus-plain (dsf)", "theseus-spine",
     "theseus-plain", "dsf", "explore"),
    ("харнесс: spine-arch − theseus-spine (dsf)", "spine-arch", "theseus-spine",
     "dsf", "explore"),
    ("харнесс: spine-arch − kimi-plain (dsf)", "spine-arch", "kimi-plain",
     "dsf", "explore"),
    ("формат: claude-spine − claude-plain (glm)", "claude-spine", "claude-plain",
     "glm", "prereg"),
    ("харнесс: spine-arch − claude-spine (glm)", "spine-arch", "claude-spine",
     "glm", "prereg"),
    ("контур против голой модели: spine-arch − raw-llm (dsf)", "spine-arch",
     "raw-llm", "dsf", "explore"),
    ("контур против голой модели: spine-arch − raw-llm (glm)", "spine-arch",
     "raw-llm", "glm", "explore"),
]


def metric_value(rec, name):
    d = rec.get("defects") or {}
    g = rec.get("gate_final") or {}
    h = rec.get("handoff") or {}
    if name == "caught":
        return (d.get("caught") or {}).get("value")
    if name == "leaked":
        return (d.get("leaked") or {}).get("value")
    if name == "alarm":
        return (d.get("control_alarm") or {}).get("value")
    if name == "rules_restored":
        rr = d.get("rules_restored") or {}
        return (rr["n"] / rr["d"]) if rr.get("d") else None
    if name == "gate_pass":
        return None if not g.get("present") or "error" in g else int(bool(g.get("passed")))
    if name == "gate_green":
        if not g.get("present") or "error" in g or not g.get("rules_total"):
            return None
        return g["rules_green"] / g["rules_total"]
    if name == "trace_cov":
        c = ((g.get("contour") or {}).get("trace") or {}).get("detail") or {}
        return (c["covered"] / c["total"]) if c.get("total") else None
    if name == "packet":
        return int(bool(h.get("packet_complete")))
    if name == "coverage":
        return (h.get("coverage") / 5) if h.get("coverage") is not None else None
    if name == "keyterm":
        return (h.get("keyterm_recall") or {}).get("value")
    if name == "resume":
        return int(bool(h.get("resume_present")))
    return None


def paired_diffs(recs, a, b, model, metric):
    ca, cb = {}, {}
    for r in recs:
        if r["model"] != model:
            continue
        v = metric_value(r, metric)
        if v is None:
            continue
        key = (r["task"], r["rep"])
        if r["arm"] == a:
            ca[key] = v
        elif r["arm"] == b:
            cb[key] = v
    common = sorted(set(ca) & set(cb))
    return [ca[k] - cb[k] for k in common]


def bootstrap_ci(diffs, rng):
    boots = sorted(statistics.mean(rng.choice(diffs) for _ in diffs)
                   for _ in range(BOOT_N))
    return boots[int(0.025 * BOOT_N)], boots[int(0.975 * BOOT_N) - 1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metric", default=None, choices=sorted(METRICS))
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    src = RUNS / "results" / "contour.jsonl"
    if not src.is_file():
        print(f"нет {src} — сначала runners/contour_score.py")
        return 1
    recs = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    # Уличенные аудитом ячейки недействительны и в парных эффектах: иначе
    # контаминация вернулась бы в публикуемые числа через чёрный ход —
    # таблица средних её не видит, а парная разность считала бы по всем.
    aud = pvlib.load_audit()
    kept = pvlib.eligible_cells(recs, aud)
    if len(kept) != len(recs):
        print(f"исключено по изоляции: {len(recs) - len(kept)} ячеек "
              f"(нарушение или нет журнала при наличии инструментов)")
    recs = kept
    print(f"ячеек: {len(recs)}; задач: {len({r['task'] for r in recs})}; "
          f"рук: {len({r['arm'] for r in recs})}")
    metrics = [a.metric] if a.metric else list(METRICS)
    rng = random.Random(SEED)
    out = {}
    for label, arm_a, arm_b, model, status in EFFECTS:
        row = {}
        for met in metrics:
            diffs = paired_diffs(recs, arm_a, arm_b, model, met)
            if not diffs:
                row[met] = None
                continue
            lo, hi = bootstrap_ci(diffs, rng)
            row[met] = {"diff": round(statistics.mean(diffs), 3),
                        "ci_lo": round(lo, 3), "ci_hi": round(hi, 3),
                        "n_pairs": len(diffs), "metric": METRICS[met],
                        "status": status}
        out[label] = row
    if a.metric:
        print(f"\nметрика: {METRICS[a.metric]}")
        print(f"{'сравнение':50s} {'Δ':>7s} {'95% CI':>18s} {'пар':>4s}")
        for label, row in out.items():
            v = row.get(a.metric)
            if not v:
                print(f"{label:50s} — нет общих ячеек")
                continue
            star = " *" if (v["ci_lo"] > 0 or v["ci_hi"] < 0) else ""
            print(f"{label:50s} {v['diff']:+7.3f} "
                  f"[{v['ci_lo']:+6.3f}; {v['ci_hi']:+6.3f}] {v['n_pairs']:>4d}{star}")
    else:
        for label, row in out.items():
            print(f"\n{label}")
            for met, v in row.items():
                if not v:
                    continue
                star = " *" if (v["ci_lo"] > 0 or v["ci_hi"] < 0) else ""
                print(f"   {METRICS[met]:46s} {v['diff']:+7.3f} "
                      f"[{v['ci_lo']:+6.3f}; {v['ci_hi']:+6.3f}] "
                      f"n={v['n_pairs']}{star}")
    dst = Path(a.json) if a.json else (RUNS / "results" / "contour_effects.json")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(
        {"method": f"парная разность по ячейкам «задача × повтор», bootstrap "
                   f"95% CI ({BOOT_N} ресэмплов, seed={SEED})",
         "source": "runners/contour_effects.py из results/contour.jsonl",
         "effects": out}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nзаписано: {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
