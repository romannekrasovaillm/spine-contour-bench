#!/usr/bin/env python3
"""judge_merge.py — слияние пула судей в единый judge.json на ячейку.

Источники вердиктов на ячейку:
  - legacy judge.json (одиночный судья; источник только если это НЕ результат
    предыдущего merge — маркер "pooled": true) — тег берётся из judge_model;
  - judge_<tag>.json — агрегаты tagged-прогонов judge.py (PVBENCH_JUDGE_TAG).

Агрегация пула (та же формула, что в judge.py):
  per-criterion mean по ВСЕМ прогонам всех судей, веса из RUBRICS.md задачи;
  total = 100*sum(w*s)/(4*sum(w)); HF — объединение по всем судьям ->
  total = min(total, 39). evidence_unverified — среднее по источникам.
Итоговый judge.json помечается "pooled": true и несёт:
  judge_model = "pool:<tag1>,<tag2>", judges = {tag: total}, k = сумма прогонов.
Анализ (analyze.py) читает те же поля, что и у одиночного судьи.

Дополнительно печатает статистику сдвига между судьями (mean/median |Δtotal|
по парам тегов) — для раздела ограничений отчёта (конфаунд судьи, D8).
Идемпотентен: перезапуск пересобирает judge.json из источников.
Только stdlib.
"""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

CELLS = pvlib.CELLS_DIR


def load_sources(cell):
    """Список (tag, aggregate-dict) по ячейке."""
    sources = []
    legacy = cell / "judge.json"
    if legacy.is_file():
        try:
            agg = json.loads(legacy.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            agg = None
        if agg and not agg.get("pooled"):
            sources.append((str(agg.get("judge_model") or "legacy"), agg))
    for p in sorted(cell.glob("judge_*.json")):
        # пропускаем файлы прогонов judge_<tag>_<k>.json и legacy judge_<k>.json
        stem = p.stem[len("judge_"):]
        if stem.rsplit("_", 1)[-1].isdigit():
            continue
        try:
            agg = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if agg and "per_criterion" in agg:
            sources.append((stem, agg))
    # дедупликация тегов (legacy-тег может совпасть с tagged-агрегатом)
    seen, out = set(), []
    for tag, agg in sources:
        if tag not in seen:
            seen.add(tag)
            out.append((tag, agg))
    return out


def merge_cell(cell):
    sources = load_sources(cell)
    if not sources:
        return None
    task = cell.name.split("__")[0]
    weights = pvlib.extract_criteria_weights(
        pvlib.load_task_files(task)["RUBRICS"])

    per_crit, ev_unv, hf_trig, judges, k_total = {}, [], set(), {}, 0
    for cid, w in weights.items():
        scores = []
        for _, agg in sources:
            try:
                scores.extend(float(s) for s in
                              agg["per_criterion"][cid]["scores"])
            except (KeyError, TypeError, ValueError):
                pass
        if scores:
            per_crit[cid] = {"mean": sum(scores) / len(scores), "weight": w,
                             "scores": scores}
    for tag, agg in sources:
        judges[tag] = agg.get("total")
        k_total += int(agg.get("k") or 0)
        hf_trig.update(agg.get("hf_triggered") or [])
        if agg.get("evidence_unverified") is not None:
            ev_unv.append(float(agg["evidence_unverified"]))
    sw = sum(w for cid, w in weights.items() if cid in per_crit)
    total = 0.0
    if sw:
        total = 100.0 * sum(c["mean"] * c["weight"]
                            for c in per_crit.values()) / (4.0 * sw)
    total_capped = min(total, 39.0) if hf_trig else total
    tags = sorted(judges)
    out = {"cell": cell.name, "task": task,
           "judge_model": "pool:" + ",".join(tags),
           "pooled": True, "judges": judges, "k": k_total,
           "per_criterion": per_crit, "total_raw": round(total, 2),
           "hf_triggered": sorted(hf_trig),
           "total": round(total_capped, 2),
           "evidence_unverified": (round(sum(ev_unv) / len(ev_unv), 4)
                                   if ev_unv else None),
           "errors": [],
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (cell / "judge.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main():
    merged, shifts = 0, {}
    for cell in sorted(p for p in CELLS.iterdir() if p.is_dir()):
        if not (cell / "answer.md").is_file():
            continue
        out = merge_cell(cell)
        if not out:
            continue
        merged += 1
        tags = sorted(out["judges"])
        for i in range(len(tags)):
            for j in range(i + 1, len(tags)):
                a, b = out["judges"][tags[i]], out["judges"][tags[j]]
                if a is None or b is None:
                    continue
                shifts.setdefault(f"{tags[i]} vs {tags[j]}",
                                  []).append(abs(a - b))
    print(f"собрано judge.json из пула: {merged} ячеек")
    for pair, ds in sorted(shifts.items()):
        print(f"сдвиг {pair}: n={len(ds)} mean|Δ|={statistics.mean(ds):.2f} "
              f"median|Δ|={statistics.median(ds):.2f} max|Δ|={max(ds):.2f}")


if __name__ == "__main__":
    main()
