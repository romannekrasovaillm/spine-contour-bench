#!/usr/bin/env python3
"""contour_score.py — метрики контура по ячейкам (без судьи).

Для каждой ячейки считает три семейства метрик и пишет
`<PVBENCH_RUNS>/results/contour.jsonl` (одна строка JSON на ячейку) и
`results/contour_summary.json`.

A. ГЕЙТЫ. Рульсет `contour/<TASK>/CONSTRAINTS.contour.yaml` прогоняется
   `arch-be control check --json` по версии v1 (answer.v1.md) и по финалу
   (answer.md): сколько правил зелёных, что нашли проверки контура.

B. ДЕФЕКТЫ. По `injection.json` и двум версиям документа:
   fact   — ложное утверждение: осталось в финале = УТЕЧКА, убрано = ПОЙМАНО;
   control— истинное утверждение: осталось = норма, убрано = ЛОЖНОЕ СРАБАТЫВАНИЕ;
   rule   — структурный дефект: восстановлен ли в финале.

C. HANDOFF. Артефакт передачи: есть ли, какие семейства содержания покрыты,
   сколько ключевых терминов решения из v1 названо в RESUME.md.

Судья здесь не участвует; качество документа считается отдельно
(runners/mech_score.py, runners/judge.py — как в platformv-arch-bench).

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/contour_score.py [--only-glob '*']
Только stdlib.
"""
import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib
import contour_metrics as cm

BASE = pvlib.BASE
CELLS = pvlib.CELLS_DIR
CONTOUR = BASE / "contour"
TMP = Path(tempfile.gettempdir()) / "contour-score"

NOT_ARTIFACT = {"TASK.md", "CONTEXT.md", "ACCEPTANCE.md", "ARCHITECTURE-SPINE.md",
                "CONSTRAINTS.yaml", "CLAUDE.md", "AGENTS.md", "RESUME.md",
                "WORK-IN-PROGRESS.md", "answer.md", "answer.v1.md",
                "answer.stage2.md", "gate_extra.py", "task_meta.json"}

FAMILIES = {
    "state":      ["что сделано", "сделано", "готово", "выполнено", "реализован"],
    "remaining":  ["осталось", "не закрыт", "открыт", "неполн", "todo", "что дальше"],
    "decisions":  ["решени", "выбран", "принят", "обоснован"],
    "risks":      ["риск", "допущен", "требует проверки", "предположен"],
    "rollback":   ["откат", "rollback", "вернуться", "восстанов", "не трогать"],
}


def _scratch(work, tag):
    """Копия рабочего каталога для прогона гейта.

    Копия, а не живой каталог: (1) проверки не должны зависеть от того, что
    рука сделала со своей копией `.contour/`; (2) гейт пишет отчёт рядом.
    """
    dst = TMP / tag / "work"
    if dst.parent.exists():
        shutil.rmtree(dst.parent, ignore_errors=True)
    shutil.copytree(work, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc"))
    # Проверяльщик — из репозитория, а не из ячейки (защита от правок руками).
    # Каталог создаём сами: у рук без пакета `.contour/` в ячейке нет вовсе —
    # они получают его только здесь, и рульсет для всех рук один и тот же.
    (dst / ".contour").mkdir(exist_ok=True)
    shutil.copy2(BASE / "contour" / "gate_extra.py",
                 dst / ".contour" / "gate_extra.py")
    return dst


def task_meta(task):
    """Список требований задачи — так же, как он попадает в ячейку руки.

    Скорер кладёт этот файл в скрипт-каталог САМ, для всех рук одинаково:
    у рук без пакета `.contour/` в ячейке нет вовсе (иначе «без формата» не
    было бы без формата), и без этого шага проверки контура падали бы у них
    всегда — метрика мерила бы наличие каталога, а не документ.
    """
    files = pvlib.load_task_files(task)
    import contour_prep as cp
    return {"task": task,
            "requirements": [{"id": rid, "kind": kind}
                             for rid, kind, _ in cp.parse_requirements(
                                 files["CONTEXT"])]}


def run_gate(cell_name, task, work, answer_src, tag):
    """Прогон рульсета контура по одной версии документа."""
    if answer_src is None or not answer_src.is_file():
        return {"present": False}
    scratch = _scratch(work, f"{cell_name}__{tag}")
    (scratch / ".contour").mkdir(exist_ok=True)
    (scratch / ".contour" / "task_meta.json").write_text(
        json.dumps(task_meta(task), ensure_ascii=False, indent=1),
        encoding="utf-8")
    shutil.copy2(answer_src, scratch / "answer.md")
    ruleset = CONTOUR / task / "CONSTRAINTS.contour.yaml"
    cmd = ["arch-be", "control", "check", ".", "--constraints", str(ruleset),
           "--json"]
    try:
        proc = subprocess.run(cmd, cwd=scratch, capture_output=True, timeout=300)
        rep = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        return {"present": True, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    # проверка `invented` в рульсет руки не входит (её список — в RUBRICS.md,
    # которого у руки нет); скорер применяет её сам, ко всем рукам одинаково
    subprocess.run(["python3", str(BASE / "contour" / "gate_extra.py"),
                    "--check", "invented", "--repo", ".", "--task", task,
                    "--tasks-dir", str(BASE / "tasks")],
                   cwd=scratch, capture_output=True, timeout=120)
    issues = rep.get("issues") or []
    summary = rep.get("summary", "")
    m = re.search(r"Правил:\s*(\d+).*?нарушений:\s*(\d+)", summary)
    total = int(m.group(1)) if m else None
    broken = int(m.group(2)) if m else None
    extra = {}
    ep = scratch.parent / "contour_extra.json"
    if ep.is_file():
        try:
            extra = json.loads(ep.read_text(encoding="utf-8"))
        except ValueError:
            extra = {}
    return {"present": True, "passed": bool(rep.get("passed")),
            "rules_total": total, "rules_broken": broken,
            "rules_green": (total - broken) if (total is not None
                                                and broken is not None) else None,
            "errors": sum(1 for i in issues if i.get("severity") == "error"),
            "warns": sum(1 for i in issues if i.get("severity") == "warn"),
            "issued_rules": sorted({i.get("rule") for i in issues if i.get("rule")}),
            "contour": {k: {"passed": v.get("passed"), "message": v.get("message"),
                            "detail": v.get("detail")}
                        for k, v in extra.items()}}


def keyterm_recall(v1_text, resume_text, top=8):
    """Доля ключевых терминов решения из v1, названных в RESUME.md."""
    if not resume_text:
        return None
    cnt = {}
    for t in re.findall(r"\b[A-Z][A-Za-z][A-Za-z0-9]{1,9}\b", v1_text):
        cnt[t] = cnt.get(t, 0) + 1
    terms = [t for t, c in sorted(cnt.items(), key=lambda kv: -kv[1])
             if c >= 2][:top]
    if not terms:
        return None
    low = resume_text.lower()
    hit = [t for t in terms if t.lower() in low]
    return {"n": len(hit), "d": len(terms),
            "value": round(len(hit) / len(terms), 3), "terms": terms}


def handoff_artifact(work):
    """Артефакты передачи в work/ (кроме служебных файлов задачи)."""
    files = []
    for p in sorted(work.rglob("*")):
        if not p.is_file():
            continue
        if p.name in NOT_ARTIFACT:
            continue
        if p.suffix.lower() not in (".md", ".txt", ".yaml", ".yml"):
            continue
        if ".contour" in p.parts:      # самодостаточный набор проверки, не передача
            continue
        rel = str(p.relative_to(work))
        if rel.startswith("transfer/") or re.search(
                r"(?i)handoff|передач|transfer|summary|state|итог|status", p.name):
            files.append(p)
    return files


def score_handoff(cell, work, v1_text):
    arts = handoff_artifact(work)
    blast = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                      for p in arts)
    fams = {k: any(kw in blast.lower() for kw in kws)
            for k, kws in FAMILIES.items()}
    resume = work / "RESUME.md"
    rtext = resume.read_text(encoding="utf-8", errors="replace") if resume.is_file() else ""
    rep = {"artifacts": [str(p.relative_to(work)) for p in arts],
           "artifact_present": bool(arts),
           "artifact_bytes": len(blast.encode("utf-8")),
           "families": fams,
           "coverage": sum(fams.values()),
           "resume_present": bool(rtext),
           "resume_bytes": len(rtext.encode("utf-8")),
           "keyterm_recall": keyterm_recall(v1_text, rtext)}
    # пакет по контракту формата (I-7): только у рук с пакетом он ожидается,
    # но проверяем у всех — отсутствие у plain и есть измеряемое различие
    tdir = work / "transfer"
    need = ["TASK-BRIEF.md", "STATE.md", "INVARIANTS.md", "ROLLBACK.md"]
    rep["packet_files"] = [f for f in need if (tdir / f).is_file()
                           and (tdir / f).stat().st_size > 0]
    rep["packet_complete"] = len(rep["packet_files"]) == len(need)
    return rep


def score_defects(cell, v1_text, final_text):
    inj = cell / "injection.json"
    if not inj.is_file():
        return {"present": False}
    man = json.loads(inj.read_text(encoding="utf-8"))
    sents1, sents2 = cm.sentences(v1_text), cm.sentences(final_text)
    items = []
    for x in man["fact"]:
        items.append(cm.item_verdict(dict(x, kind="fact"), final_text, sents2))
    for x in man["control"]:
        items.append(cm.item_verdict(dict(x, kind="control"), final_text, sents2))
    rules = []
    for r in man["rule"]:
        restored = cm.rule_restored(r, final_text, v1_text)
        rules.append({"id": r["id"], "op": r["op"],
                      "skipped": r.get("skipped"), "restored": restored})
    out = {
        "present": True,
        "caught": cm.rate(items, "caught", ("fact",)),
        "leaked": cm.rate(items, "leaked", ("fact",)),
        "control_preserved": cm.rate(items, "preserved", ("control",)),
        "control_alarm": cm.rate(items, "false_alarm", ("control",)),
        "rules_restored": {"n": sum(1 for r in rules if r["restored"] is True),
                           "d": sum(1 for r in rules if r["restored"] is not None),
                           "detail": rules},
        "items": items,
    }
    # дефекты, уже присутствовавшие в v1 до инъекции (диагностика)
    out["pre_existing_fact"] = sum(
        1 for x in man["fact"]
        if cm.item_verdict(dict(x, kind="fact"), v1_text, sents1)["verdict"]
        == "leaked")
    out["pre_existing_control"] = sum(
        1 for x in man["control"]
        if cm.item_verdict(dict(x, kind="control"), v1_text, sents1)["verdict"]
        == "false_alarm")
    return out


def _meta2(cell, key, default=None):
    """Поле из meta2.json стадии 2 (или default, если файла нет/он битый)."""
    p = cell / "meta2.json"
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(key, default)
    except ValueError:
        return default


def score_cell(cell):
    m = re.match(r"^(.+?)__(.+)__(dsf|glm)__r(\d+)$", cell.name)
    if not m:
        return None
    task, arm, model, rep = m.group(1), m.group(2), m.group(3), int(m.group(4))
    work = cell / "work"
    # Обе версии ищутся и в work/, и на уровне ЯЧЕЙКИ:
    #   * v1 инъекция кладёт на уровень ячейки намеренно — копия внутри work/
    #     давала преемнику нетронутый документ, к которому можно откатиться
    #     вместо разбора v1.1 (D8). Скорер, искавший только work/answer.v1.md,
    #     молча подставлял вместо v1 финал: тогда v1 == финал, разница гейтов
    #     v1→финал и «восстановлено структурных дефектов» обнулялись у ВСЕХ
    #     ячеек — то есть ключевая метрика контура не измерялась вовсе;
    #   * финал рука вправе сдать в полученном документе, не создавая
    #     answer.md заново (так делает theseus); раннер кладёт его в
    #     cell/answer.md.
    v1p = work / "answer.v1.md"
    if not v1p.is_file():
        v1p = cell / "answer.v1.md"
    finp = work / "answer.md"
    if not finp.is_file():
        finp = cell / "answer.md"
    v1 = v1p.read_text(encoding="utf-8", errors="replace") if v1p.is_file() \
        else (finp.read_text(encoding="utf-8", errors="replace")
              if finp.is_file() else "")
    final = finp.read_text(encoding="utf-8", errors="replace") if finp.is_file() else ""
    # ФИНАЛА НЕТ, если преемник не тронул документ, а процесс вышел нештатно.
    # Такой «финал» — это нетронутый v1.1: сборщик собирает его, чтобы ячейка
    # не выглядела пустой, но работы преемника в нём нет. Считать по нему
    # метрики значило бы записать руке нули за исчерпанный бюджет или отказ
    # среды (D20, D22) — то есть выдать поломку за свойство контура.
    # Отличается от «отработал и не тронул» (exit 0): вот это — законный
    # нулевой зачёт, и он остаётся.
    if (_meta2(cell, "deliverable_source") == "wip"
            and not _meta2(cell, "wip_modified")
            and (_meta2(cell, "exit_code") or 0) != 0):
        final = ""
    # И ФИНАЛ, КОТОРЫЙ НЕ ДОКУМЕНТ, финалом не считается. Сборщик принимает за
    # работу любой текст длиннее 500 байт — включая СООБЩЕНИЕ о работе. Так у
    # одной ячейки (SYN-ARCH-001__spine-arch__dsf__r1) «документом» оказался
    # вопрос к пользователю: рука в неинтерактивном режиме упёрлась в решение
    # (превышение лимита слов) и спросила вместо того, чтобы решить. Это тот же
    # класс, что D10 (raw-llm «отчитался» о создании файла). Критерий
    # механический и проверяемый: у документа задачи есть заголовки разделов, и
    # он не в двести слов. Считать по такому тексту метрики значило бы мерить
    # контур по вопросу.
    if final.strip():
        heads = len(re.findall(r"(?m)^#{1,3} \S", final))
        if heads < 3 and len(final.split()) < 1000:
            final = ""
    rec = {
        "cell": cell.name, "task": task, "arm": arm, "model": model, "rep": rep,
        "stage2": (cell / "meta2.json").is_file(),
        # Как именно сдана стадия 2 — это часть чтения результата, а не
        # служебная мелочь: у theseus финал лежит в полученном документе
        # (`wip`), а не в answer.md, и у него же встречались отказы среды
        # (HTTP 400 от вендора, D20). Без этих полей «рука ничего не сделала»
        # и «рука не смогла начать» в выгрузке неразличимы.
        "deliverable_source": _meta2(cell, "deliverable_source"),
        "wip_modified": _meta2(cell, "wip_modified"),
        "stage2_error": bool(_meta2(cell, "error")),
        "stage2_secs": _meta2(cell, "secs"),
        # Код возврата процесса стадии 2. Отдельно от `stage2_error`: у ячеек,
        # перегнанных ДО правки сборщика (D17/D20), ошибка не сохранялась, и
        # «рука отработала, но документ не тронула» выглядело бы как чистая
        # сдача. Метрику это не завышает (детектор считает по тексту), но
        # читателю нужно видеть, что процесс завершился нештатно.
        "stage2_exit": _meta2(cell, "exit_code"),
        "v1_words": pvlib.count_words(v1) if v1 else None,
        "final_words": pvlib.count_words(final) if final else None,
        "gate_v1": run_gate(cell.name, task, work,
                            v1p if v1p.is_file() else None, "v1"),
        "gate_final": run_gate(cell.name, task, work,
                               finp if finp.is_file() else None, "final"),
        "defects": score_defects(cell, v1, final),
        "handoff": score_handoff(cell, work, v1),
    }
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-glob", default="*")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    cells = [p for p in sorted(CELLS.iterdir())
             if p.is_dir() and fnmatch.fnmatch(p.name, a.only_glob)]
    recs = []
    for c in cells:
        r = score_cell(c)
        if r:
            recs.append(r)
            print(f"{r['cell']}: gate_v1={_g(r['gate_v1'])} "
                  f"gate_fin={_g(r['gate_final'])} "
                  f"defects={r['defects'].get('caught', {}).get('value')}/"
                  f"{r['defects'].get('leaked', {}).get('value')} "
                  f"packet={r['handoff'].get('coverage')}/5", flush=True)
    if not getattr(a, "json", None):
        pass
    out = Path(a.json) if a.json else (pvlib.RUNS / "results" / "contour.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs)
                   + "\n", encoding="utf-8")
    print(f"\nзаписано: {out} (ячеек: {len(recs)})")
    return 0


def _g(g):
    if not g.get("present"):
        return "—"
    if "error" in g:
        return "ERR"
    return f"{g['rules_green']}/{g['rules_total']}" + ("✓" if g["passed"] else "✗")


if __name__ == "__main__":
    sys.exit(main())
