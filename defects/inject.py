#!/usr/bin/env python3
"""inject.py — детерминированная порча работы между стадией 1 и стадией 2.

Ход протокола:

  стадия 1  агент пишет work/answer.md
  inject    answer.md → work/WORK-IN-PROGRESS.md (v1.1) с внесёнными дефектами,
            answer.md → answer.v1.md (не теряется), пишется injection.json
  стадия 2  преемник получает v1.1 и артефакт передачи, пишет финальный answer.md

Инъекции (состав фиксирован константами ниже и заморожен в PREREGISTRATION):

  fact      N_FACT ложных утверждений из таблицы ловушек RUBRICS.md §4
            («уточнения смежной команды», поданные как факт);
  control   N_CTRL ИСТИННЫХ утверждений оттуда же — контроль ложных
            срабатываний: преемник, «исправляющий» верный тезис, ошибается;
  rule      по одному структурному дефекту на каждую проверку контура:
            drop_section (снят заголовок обязательного раздела),
            break_trace (вычищены строки трассировки),
            invented (в текст вписан вымышленный компонент из HF-01).

Состав инъекций одинаков для всех рук и повторов — сравнение парное.

Использование:
  python3 defects/inject.py <cell_dir> --task <TASK>
  python3 defects/inject.py --self-test <cell_dir> --task <TASK>
Только stdlib.
"""
import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "runners"))
import contour_metrics as cm          # noqa: E402
import contour_prep as cp             # noqa: E402

N_FACT = 6
N_CTRL = 3
UPDATE_HEADING = "## Обновление v1.1 — уточнения смежной команды"

FILLER = (
    "Правки внесены смежной командой в рамках планового уточнения "
    "требований и не проходили архитектурный контроль. "
)


def select(task):
    """Замороженный состав инъекций для задачи (одинаков для всех ячеек)."""
    rec = cp.gen_defects(task)
    fact = [dict(x, kind="fact") for x in rec["fact"][:N_FACT]]
    ctrl = [dict(x, kind="control") for x in rec["control"][:N_CTRL]]
    rules = []
    # 1. снятый заголовок обязательного раздела
    tgt = None
    for r in rec["rule"]:
        if r["op"] == "drop_section":
            tgt = r["target"]
    if tgt:
        rules.append({"id": "R-drop", "op": "drop_section", "target": tgt,
                      "kind": "rule"})
    # 2. порванная трассировка
    rules.append({"id": "R-trace", "op": "break_trace", "target": 3,
                  "kind": "rule"})
    # 3. вымышленный компонент (если у задачи есть примеры HF-01)
    if rec["invented"]:
        rules.append({"id": "R-invented", "op": "invented",
                      "claim": rec["invented"][0], "kind": "rule"})
    return {"task": task, "fact": fact, "control": ctrl, "rule": rules}


def _strip_quotes(s):
    return s.strip().strip("«»\"' ").strip()


def drop_section(text, target):
    """Снимает строку-заголовок раздела с меткой (D<n>)."""
    pat = re.compile(rf"(?m)^(#{{1,4}}[^\n]*\({re.escape(target)}\)[^\n]*)$")
    m = pat.search(text)
    if not m:
        pat = re.compile(rf"(?m)^(#{{1,4}}[^\n]*\b{re.escape(target)}\b[^\n]*)$")
        m = pat.search(text)
    if not m:
        return text, None
    heading = m.group(1)
    return text[:m.start()] + text[m.end():].lstrip("\n"), heading


def break_trace(text, n):
    """Вычищает первые n строк трассировки табличного или стрелочного вида."""
    lines = text.splitlines()
    row = re.compile(r"^\|\s*\**\s*((INF|NFR|SEC)-\d+)\s*\**\s*\|")
    arrow = re.compile(r"^\s*[-*>|]*\s*\**\s*((INF|NFR|SEC)-\d+)\b[^\n]*?(->|→)")
    ids, kept = [], []
    for line in lines:
        m = row.match(line) or arrow.match(line)
        if m and len(ids) < n:
            ids.append(m.group(1))
            continue
        kept.append(line)
    return "\n".join(kept) + "\n", ids


def add_invented(text, claim):
    """Вписывает вымышленный компонент как факт в конец документа."""
    block = (f"\n\n### Использованные компоненты (уточнение)\n\n"
             f"В решении задействован {claim} — он закрывает маршрутизацию "
             f"между ЦОД без дополнительных надстроек.\n")
    return text + block


def inject(cell: Path, task: str, selection=None):
    sel = selection or select(task)
    work = cell / "work"
    ans = work / "answer.md"
    if not ans.is_file():
        return {"ok": False, "reason": "work/answer.md отсутствует (стадия 1 не сдана)"}
    v1 = ans.read_text(encoding="utf-8", errors="replace")
    # Версия стадии 1 сохраняется ТОЛЬКО на уровне ячейки, вне рабочего
    # каталога. Копия внутри work/ давала преемнику нетронутый документ,
    # к которому можно просто откатиться вместо разбора v1.1 — на пилоте так
    # и произошло в одной из ячеек (md5 финала совпал с md5 v1). Преемник
    # должен видеть ровно то, что ему передали: v1.1.
    (cell / "answer.v1.md").write_text(v1, encoding="utf-8")

    mutated = v1
    applied = []

    for r in sel["rule"]:
        if r["op"] == "drop_section":
            mutated, heading = drop_section(mutated, r["target"])
            if heading:
                applied.append(dict(r, heading=heading))
            else:
                applied.append(dict(r, heading=None, skipped="заголовок не найден"))
        elif r["op"] == "break_trace":
            mutated, ids = break_trace(mutated, int(r["target"]))
            applied.append(dict(r, ids=ids,
                                skipped=None if ids else "строк трассировки нет"))
        elif r["op"] == "invented":
            mutated = add_invented(mutated, r["claim"])
            applied.append(r)

    bullets = [f"- {_strip_quotes(x['claim'])} — принято к сведению."
               for x in sel["fact"] + sel["control"]]
    block = ("\n\n" + UPDATE_HEADING + "\n\n" + FILLER + "\n\n"
             + "\n".join(bullets) + "\n")
    mutated = mutated.rstrip() + block

    wip = work / "WORK-IN-PROGRESS.md"
    wip.parent.mkdir(parents=True, exist_ok=True)
    wip.write_text(mutated, encoding="utf-8")
    # answer.md убирается: иначе преемник может просто оставить версию стадии
    # 1 нетронутой, и «финал» окажется не его работой. Писать финал он обязан
    # сам (см. prompt2.txt).
    ans.unlink()

    manifest = {"task": task, "n_fact": len(sel["fact"]),
                "n_control": len(sel["control"]),
                "fact": sel["fact"], "control": sel["control"],
                "rule": applied,
                "wip": "work/WORK-IN-PROGRESS.md",
                "v1": "work/answer.v1.md"}
    (cell / "injection.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, **{k: len(v) if isinstance(v, list) else v
                           for k, v in manifest.items() if k != "task"}}


def self_test(cell: Path, task: str):
    """Проверка, что инъекция валит контур, а контрольный класс не пуст."""
    sel = select(task)
    v1 = (cell / "answer.v1.md").read_text(encoding="utf-8", errors="replace")
    wip = (cell / "work" / "WORK-IN-PROGRESS.md").read_text(encoding="utf-8")
    res = {"task": task, "n_fact": len(sel["fact"]),
           "n_control": len(sel["control"]), "n_rule": len(sel["rule"])}
    # 1. дефекты различимы в испорченном документе (детектор работает)
    sents = cm.sentences(wip)
    facts = [cm.item_verdict(x, wip, sents) for x in sel["fact"]]
    ctrls = [cm.item_verdict(x, wip, sents) for x in sel["control"]]
    res["fact_detectable"] = sum(1 for f in facts if f["state"] == "retained")
    res["control_detectable"] = sum(1 for c in ctrls if c["state"] == "retained")
    # 2. контрольный класс не срабатывает на НЕиспорченном документе
    #    (иначе detector ловит всё подряд)
    s1 = cm.sentences(v1)
    res["fact_in_v1"] = sum(1 for x in sel["fact"]
                            if cm.item_verdict(x, v1, s1)["state"] == "retained")
    res["control_in_v1"] = sum(1 for x in sel["control"]
                               if cm.item_verdict(x, v1, s1)["state"] == "retained")
    # 3. правила-класс действительно применились
    res["rules_applied"] = sum(1 for r in json.loads(
        (cell / "injection.json").read_text(encoding="utf-8"))["rule"]
        if not r.get("skipped"))
    res["ok"] = (res["fact_detectable"] == res["n_fact"]
                 and res["control_detectable"] == res["n_control"]
                 and res["fact_in_v1"] == 0
                 and res["rules_applied"] > 0)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cell")
    ap.add_argument("--task", required=True)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    cell = Path(a.cell).resolve()
    if a.self_test:
        try:
            inject(cell, a.task)
        except Exception:  # noqa: BLE001
            pass
        out = self_test(cell, a.task)
    else:
        out = inject(cell, a.task)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
