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


def load_reruns():
    """Семьи перегонов с основаниями (results/reruns.json)."""
    p = RUNS / "results" / "reruns.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def load_audit():
    """{ячейка: запись аудита} — общая с contour_effects (pvlib.load_audit)."""
    return pvlib.load_audit()


def cell_status(cell, aud):
    """Годность ячейки по изоляции (pvlib.iso_status)."""
    return pvlib.iso_status(cell, aud)


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
    aud = load_audit()
    for r in recs:
        r["iso"] = cell_status(r["cell"], aud)
    n_bad = sum(1 for r in recs if r["iso"] == "нарушение")
    n_unver = sum(1 for r in recs if r["iso"] == "непроверяемая")
    scored = [r for r in recs if r["iso"] in ("чистая", "без аудита")]

    L = ["# Результаты: метрики контура", "",
         f"Ячеек в выгрузке: **{len(recs)}**; задач: "
         f"{len({r['task'] for r in recs})}; рук: {len({r['arm'] for r in recs})}.",
         "",
         "Метрики — средние по ячейкам руки (в скобках — число ячеек, по которым "
         "метрика определена; метрики дефектов и handoff считаются только на "
         "ячейках с запущенной стадией 2). Таблицы разведены ПО МОДЕЛЯМ: модель — "
         "одна из трёх осей, и усреднять её внутри руки нельзя, иначе частичное "
         "покрытие одной модели читается как вклад формата.", ""]
    if n_bad or n_unver:
        L += [f"**В средние не входят ячейки с нарушением изоляции — "
              f"{n_bad}, непроверяемых — {n_unver}** (перечислены в разделе "
              "«Изоляция» ниже). Это не чистка результата: ячейка, чья рука "
              "читала ключ, измеряет не то, что задумано, и её место в "
              "среднем не должно занимать никакое число.", ""]

    # Таблицы РАЗВЕДЕНЫ ПО МОДЕЛЯМ. Модель — одна из трёх осей, и усреднять её
    # внутри руки нельзя: если у одной руки glm-половина выпала (внешняя
    # квота, D24), среднее по руке начинает сравнивать dsf-только с dsf+glm,
    # и разница читается как вклад формата, хотя это разница покрытия.
    arms = sorted({r["arm"] for r in recs})
    models = sorted({r["model"] for r in recs})
    for model in models:
        sub = [r for r in scored if r["model"] == model]
        L += [f"### Модель `{model}`", "",
              "| Метрика | " + " | ".join(arms) + " |",
              "|---" * (len(arms) + 1) + "|"]
        for key, label in METRICS:
            cells = []
            for arm in arms:
                m, n = mean([value(r, key) for r in sub if r["arm"] == arm])
                cells.append(f"{m if m is not None else '—'}"
                             + (f" ({n})" if m is not None else ""))
            L.append(f"| {label} | " + " | ".join(cells) + " |")
        L.append("")

    if effects:
        L += ["## Парные эффекты", "", f"Метод: {effects.get('method','')}.", "",
              "`*` — доверительный интервал не пересекает ноль.", ""]
        L += ["Пары с пометкой «зарегистрирована» названы в "
              "`PREREGISTRATION.md` (H4); остальные — исследовательские, и "
              "их нельзя читать как подтверждение гипотезы.", ""]
        for label, row in effects.get("effects", {}).items():
            st = next((v.get("status") for v in row.values()
                       if isinstance(v, dict) and v.get("status")), None)
            mark = {  "prereg": " · **зарегистрирована**",
                      "explore": " · исследовательская"}.get(st, "")
            L.append(f"**{label}**{mark}")
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

    # Как сдана стадия 2: у theseus финал лежит в полученном документе, и это
    # надо видеть отдельно — иначе «сдал иначе» читается как «не сдал».
    src = {}
    for r in recs:
        if r.get("stage2"):
            k = r.get("deliverable_source") or "—"
            src[k] = src.get(k, 0) + 1
    err = sum(1 for r in recs if r.get("stage2_error"))
    # Ячейки, где процесс завершился нештатно, но финал всё же собран из
    # полученного документа: это не «сдал», а «отработал и не тронул».
    odd = sum(1 for r in recs
              if r.get("stage2") and (r.get("stage2_exit") or 0) != 0
              and r.get("stage2_error") is None)
    if src:
        L.append("- стадия 2 по источнику финала: "
                 + ", ".join(f"`{k}` — {v}" for k, v in sorted(src.items())))
    if err:
        L.append(f"- ячеек стадии 2, завершившихся ошибкой среды: **{err}** "
                 "(это не «рука ничего не сделала»: разбор в `DEVIATIONS.md`)")
    if odd:
        L.append(f"- ячеек, где процесс стадии 2 вышел ненулевым кодом, а финал "
                 f"собран из полученного документа: **{odd}** (документ не "
                 "тронут — зачёт по нему нулевой, но это состояние надо "
                 "видеть, а не выводить из молчания)")
    if src or err:
        L.append("")

    # Изоляция — часть результата, а не сноска: кто именно ходил за ключом,
    # когда он был доступен, характеризует харнесс не меньше, чем метрики.
    L += ["## Изоляция", ""]
    if not aud:
        L += ["Аудита нет — `runners/audit_isolation.py` не прогнан. "
              "Читать метрики без него нельзя: неизвестно, кто видел ключ.", ""]
    else:
        st = {}
        for r in recs:
            st.setdefault(r["iso"], []).append(r["cell"])
        L += ["| состояние | ячеек |", "|---|---|"]
        for k in ("чистая", "попытка", "нарушение", "непроверяемая",
                  "без аудита"):
            if st.get(k):
                L.append(f"| {k} | {len(st[k])} |")
        L.append("")
        if st.get("нарушение"):
            L += ["**Ячейки с зафиксированным доступом к дизайну** (в зачёт не "
                  "идут; перегоняются, перегон исключается повторным "
                  "аудитом):", ""]
            by_arm = {}
            for c in st["нарушение"]:
                by_arm.setdefault(c.split("__")[1], []).append(c)
            for arm in sorted(by_arm):
                L.append(f"- `{arm}` ({len(by_arm[arm])}): "
                         + ", ".join(f"`{c.split('__')[0]}`"
                                     for c in sorted(by_arm[arm])))
            L.append("")
        if st.get("попытка"):
            L += ["**Попытки при работающем заслоне** (в зачёт идут: рука "
                  "ничего не узнала; публикуется как свойство харнесса — "
                  "кто ходит за ключом, когда он лежит открыто):", ""]
            L.append("- " + ", ".join(f"`{c}`" for c in sorted(st["попытка"])))
            L.append("")
        if st.get("непроверяемая"):
            L += ["**Непроверяемые** (инструменты есть, журнала нет — "
                  "отсутствие данных не есть отсутствие нарушений):", ""]
            L.append("- " + ", ".join(f"`{c}`" for c in
                                      sorted(st["непроверяемая"])))
            L.append("")
        L += ["Журналов нет у 24 ячеек `raw-llm` — у этой руки нет "
              "инструментов и файловой системы, читать ключ ей нечем по "
              "построению, поэтому они считаются чистыми не по молчанию "
              "журнала, а по устройству руки.", ""]

    # Перегоны: читателю нужно видеть не только ЧТО посчитано, но и по какой
    # причине часть ячеек считалась заново — и менялись ли при этом условия.
    rr = load_reruns()
    if rr:
        L += ["## Перегоны", "",
              "Часть ячеек посчитана заново. Основания разные по природе: "
              "контаминация — свойство прогона, а два последних пункта — "
              "дефекты сборки, найденные и исправленные по ходу (разбор в "
              "`DEVIATIONS.md`). Пересечения нормальны: одна ячейка может "
              "быть недействительна по двум причинам.", "",
              "| основание | ячеек | условие изменено |", "|---|---|---|"]
        for f in rr.get("families", []):
            ch = "**да**" if f.get("condition_changed") else "нет"
            n = len(f.get("cells") or [])
            L.append(f"| {f.get('reason', '')} | {n} | {ch} |")
        L += ["", rr.get("note", ""), ""]
        ex = rr.get("excluded_not_rerun")
        if ex:
            L += [f"**Не перегоняются:** {ex.get('reason', '')}", "",
                  f"{ex.get('rule', '')}", ""]
        for f in rr.get("families", []):
            if f.get("note"):
                L += [f"* {f.get('reason', '')[:40]}… — {f['note']}", ""]
    out = Path(a.out) if a.out else (RUNS / "results" / "summary.md")
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"записано: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
