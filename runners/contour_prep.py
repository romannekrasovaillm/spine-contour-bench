#!/usr/bin/env python3
"""contour_prep.py — генерация артефактов контура по каждой задаче.

Артефакты (в contour/<TASK>/, идемпотентно, после freeze не меняются):

  ACCEPTANCE.md            — замороженная планка приёмки: таблицы требований
                             INF-xx / NFR-xx / SEC-xx из CONTEXT.md §2,
                             список deliverables D1..Dn из TASK.md §3,
                             hard-fail правила из RUBRICS.md §2, лимит слов.
                             Всё извлечено из файлов АВТОРОВ задачи — не из
                             головы составителя бенчмарка.
  DEFECTS.yaml             — дефектный корпус: строки таблицы ловушек
                             RUBRICS.md §4 (✗ → дефект-факт, ✓ → контроль)
                             плюс правила-класс (снятая секция, порванная
                             трасса). Детерминированные детекторы.
  handoff_quiz.yaml        — вопросы преемнику и замороженные ключи.
  CONSTRAINTS.contour.yaml — рульсет гейта: базовые механические проверки
                             (как в platformv-arch-bench) + проверки контура
                             через contour/gate_extra.py.

Только stdlib.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

CONTOUR = BASE / "contour"
GATE_EXTRA = BASE / "contour" / "gate_extra.py"

# ---------------------------------------------------------------------------
# 1. Извлечение авторских входов


def parse_requirements(context_md):
    """Требования из CONTEXT.md: строки '| INF-01 | требование | … |'.

    Возвращает [(rid, kind, text)] в порядке появления (INF → NFR → SEC).
    """
    out, seen = [], set()
    for m in re.finditer(r"(?m)^\|\s*(INF|NFR|SEC)-(\d+)\s*\|(.+?)\|\s*$",
                         context_md):
        rid = f"{m.group(1)}-{m.group(2)}"
        if rid in seen:
            continue
        cells = [re.sub(r"\s+", " ", c).strip(" *")
                 for c in m.group(3).split("|")]
        cells = [c for c in cells if c and not set(c) <= set("-: ")]
        if not cells:
            continue
        seen.add(rid)
        out.append((rid, m.group(1), " — ".join(cells)))
    return out


def parse_traps(rubrics_md):
    """Таблица §4 RUBRICS.md: '| T1 | утверждение | ✗ (HF-02) | пояснение |'."""
    m = re.search(r"(?ms)^## 4\..*?ловушек.*?$(.*?)(?=^## )", rubrics_md)
    if not m:
        m = re.search(r"(?ms)^## 4\..*?$(.*?)(?=^## )", rubrics_md)
    if not m:
        return []
    out = []
    for row in re.finditer(r"(?m)^\|\s*(T\d+)\s*\|(.+?)\|\s*$", m.group(1)):
        cells = [c.strip() for c in row.group(2).split("|")]
        if len(cells) < 2:
            continue
        claim, verdict = cells[0], cells[1]
        note = cells[2] if len(cells) > 2 else ""
        if "✗" in verdict:
            v = "false"
        elif "✓" in verdict:
            v = "true"
        else:
            continue  # неразмеченная строка — пропускаем, не угадываем
        if not claim or not claim.strip("«» "):
            continue
        out.append({"id": row.group(1), "claim": re.sub(r"\s+", " ", claim),
                    "verdict": v, "note": re.sub(r"\s+", " ", note)})
    return out


def extract_invented_examples(rubrics_md):
    """HF-01: примеры вымышленных компонентов в кавычках-ёлочках."""
    m = re.search(r"(?m)^\|\s*HF-01\s*\|(.+?)\|\s*$", rubrics_md)
    if not m:
        return []
    return [re.sub(r"\s+", " ", x).strip()
            for x in re.findall(r"«([^»]+)»", m.group(1)) if len(x.strip()) > 3]


def max_words(task_md, mech):
    if mech.get("max_words"):
        return int(mech["max_words"])
    m = re.search(r"max_output_words:\s*(\d+)", task_md)
    return int(m.group(1)) if m else 4000


# ---------------------------------------------------------------------------
# 2. ACCEPTANCE.md — независимая планка приёмки


def gen_acceptance(task):
    files = pvlib.load_task_files(task)
    reqs = parse_requirements(files["CONTEXT"])
    dels = pvlib.extract_deliverables(files["TASK"])
    hfs = pvlib.extract_hard_fails(files["RUBRICS"])
    mech, _ = pvlib.load_mech(task)
    mw = max_words(files["TASK"], mech)
    kinds = []
    for rid, kind in [(r[0], r[1]) for r in reqs]:
        if kind not in kinds:
            kinds.append(kind)

    L = [f"# ACCEPTANCE.md — планка приёмки задачи {task}", "",
         "Файл **только для чтения**: он заморожен до прогона (sha256 в",
         "`prereg.lock.json`) и одинаков для всех рук. Собран из файлов авторов",
         "задачи, а не написан составителем бенчмарка:",
         "",
         "- требования — CONTEXT.md §2.3–2.5 (таблицы `INF-xx`/`NFR-xx`/`SEC-xx`);",
         "- артефакты — TASK.md §3;",
         "- hard-fail — RUBRICS.md §2;",
         "- лимит объёма — TASK.md frontmatter.",
         "",
         "## 1. Требования, подлежащие трассировке", "",
         f"Всего **{len(reqs)}** требований"
         + (f" ({', '.join(k + '-' + str(sum(1 for r in reqs if r[1] == k)) for k in kinds)})"
            if kinds else "") + ".",
         "",
         "| ID | Требование |", "|---|---|"]
    for rid, _kind, text in reqs:
        L.append(f"| {rid} | {text} |")
    L += ["", "Для каждого требования в итоговом документе должна быть строка",
          "трассировки одного из видов:", "",
          "```",
          "<ID> -> <адрес в документе>          # раздел/подраздел, где требование закрыто",
          "<ID> -> unverifiable: <причина>      # закрыть нельзя и объяснено почему",
          "```",
          "",
          "`unverifiable` без причины не засчитывается: это не статус, а отказ",
          "от трассировки.", "",
          "## 2. Обязательные артефакты", "", "| ID | Артефакт |", "|---|---|"]
    for did, desc in dels:
        L.append(f"| {did} | {desc} |")
    L += ["", "## 3. Hard-fail (итог ограничивается)", "",
          "| ID | Правило |", "|---|---|"]
    for hid, rule in hfs:
        L.append(f"| {hid} | {rule} |")
    if not hfs:
        L.append("| — | в RUBRICS.md §2 правила не перечислены |")
    L += ["", "## 4. Прочие ограничения", "",
          f"- Объём: не более **{mw}** слов (таблицы и код диаграмм не считаются).",
          "- Язык: русский; англоязычные технические термины допустимы.",
          ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 3. DEFECTS.yaml — корпус инъекций


ROLE_HINTS = ("брокер", "шлюз", "хранилищ", "секрет", "кворум", "кластер",
              "топик", "партиц", "реплик", "транзакц", "шардир", "exactly-once")


def gen_defects(task):
    """Дефект-корпус из таблицы ловушек + правила-класс."""
    files = pvlib.load_task_files(task)
    traps = parse_traps(files["RUBRICS"])
    mech, _ = pvlib.load_mech(task)
    mw = max_words(files["TASK"], mech)
    dels = pvlib.extract_deliverables(files["TASK"])

    fact = [t for t in traps if t["verdict"] == "false"]
    ctrl = [t for t in traps if t["verdict"] == "true"]

    rec = {
        "task": task,
        "source": "RUBRICS.md §4 (таблица ловушек авторов задачи)",
        "fact": [{"id": t["id"], "claim": t["claim"], "note": t["note"]}
                 for t in fact],
        "control": [{"id": t["id"], "claim": t["claim"], "note": t["note"]}
                    for t in ctrl],
        "rule": [
            {"id": "R-drop-section",
             "op": "drop_section",
             "target": dels[-3][0] if len(dels) >= 3 else "D3",
             "note": "снят заголовок обязательного раздела из v1.1"},
            {"id": "R-break-trace",
             "op": "break_trace",
             "target": 3,
             "note": "вычищены первые N строк трассировки"},
            {"id": "R-over-words",
             "op": "over_words",
             "target": mw + 400,
             "note": "дописан объёмный раздел сверх лимита слов"},
        ],
        "invented": extract_invented_examples(files["RUBRICS"]),
    }
    return rec


def defects_yaml(rec):
    """Ручная сериализация (stdlib, без PyYAML)."""
    def q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    L = [f"# DEFECTS.yaml — дефектный корпус {rec['task']}",
         f"# Источник: {rec['source']}",
         "# Заморожен до прогона; инъекция — defects/inject.py.",
         "task: " + rec["task"],
         "fact:            # ✗ строки: ложное утверждение, поданное как факт"]
    for d in rec["fact"]:
        L += [f"  - id: {d['id']}",
              f"    claim: {q(d['claim'])}",
              f"    note: {q(d['note'])}"]
    L += ["control:         # ✓ строки: ИСТИННОЕ утверждение (контроль ложных срабатываний)"]
    for d in rec["control"]:
        L += [f"  - id: {d['id']}",
              f"    claim: {q(d['claim'])}",
              f"    note: {q(d['note'])}"]
    L += ["rule:            # правила-класс: ломаем структуру, а не факт"]
    for d in rec["rule"]:
        L += [f"  - id: {d['id']}", f"    op: {d['op']}",
              f"    target: {d['target']}", f"    note: {q(d['note'])}"]
    L += ["invented:        # HF-01: примеры вымышленных компонентов из RUBRICS.md"]
    if rec["invented"]:
        for s in rec["invented"]:
            L.append(f"  - {q(s)}")
    else:
        L.append("  []")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# 4. handoff_quiz.yaml — вопросы преемнику


def gen_quiz(task):
    """Викторина преемника — про СОСТОЯНИЕ работы, не про требования задачи.

    Требования (разделы D1..Dn, лимит слов, hard-fail, группы требований)
    преемник может прочитать в work/ у любой руки — спрашивать про них
    значило бы измерять не передачу, а умение открыть TASK.md. Поэтому
    вопросы только про то, чего в файлах задачи нет: что уже решено, что
    осталось, какие допущения приняты, куда откатываться.
    """
    return {
        "task": task,
        "verifier": [
            {"id": "Q1",
             "q": "Что уже сделано предыдущим исполнителем и какие решения "
                  "приняты (по пунктам, со ссылкой на разделы документа)?"},
            {"id": "Q2",
             "q": "Что осталось незакрытым: какие разделы неполны, какие "
                  "вопросы открыты?"},
            {"id": "Q3",
             "q": "Какие допущения приняты и какие факты требуют проверки "
                  "у вендора?"},
            {"id": "Q4",
             "q": "Что делать, если принятое решение окажется неверным: "
                  "какой план отката и какие границы «не трогать»?"},
        ],
        "mechanical": {
            "keyterm_recall": "доля ключевых терминов решения из v1 (топ "
                              "компонентов), названных в RESUME.md",
            "handoff_coverage": "сколько из пяти семейств содержания "
                                "(что сделано / что осталось / решения / "
                                "риски-допущения / откат) есть в артефакте "
                                "передачи",
        },
    }


def quiz_yaml(rec):
    def q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    L = [f"# handoff_quiz.yaml — викторина преемника {rec['task']}",
         "# Вопросы про состояние работы; сверяет независимый проверяющий.",
         "# Механические метрики (keyterm_recall, handoff_coverage) считает",
         "# runners/contour_score.py — они не требуют судьи.",
         f"task: {rec['task']}", "verifier:"]
    for item in rec["verifier"]:
        L += [f"  - id: {item['id']}", f"    q: {q(item['q'])}"]
    L += ["mechanical:"]
    for k, v in rec["mechanical"].items():
        L += [f"  {k}: {q(v)}"]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# 5. CONSTRAINTS.contour.yaml — рульсет гейта


def gen_ruleset(task):
    """Базовые правила (как в platformv-arch-bench) + правила контура."""
    spec = importlib.util.spec_from_file_location(
        "pc", str(Path(__file__).resolve().parent / "prepare_cells.py"))
    pc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pc)
    _spine_md, base = pc.gen_spine_pack(task)

    header = [
        f"# CONSTRAINTS.contour.yaml — рульсет гейта задачи {task}.",
        "# Базовые правила сгенерированы из mechanical_checks RUBRICS.md",
        "# (как в platformv-arch-bench) — общий знаменатель для всех рук.",
        "# Правила контура — через contour/gate_extra.py (трассировка,",
        "# вымышленные компоненты, разрешимость адресов трассы).",
        "# Проверка: arch-be control check . --constraints CONSTRAINTS.contour.yaml",
        "",
    ]
    extra = []
    # Проверка `invented` в рульсет НЕ входит: её список примеров лежит в
    # RUBRICS.md, которого у руки нет, и подсказывать его нельзя. Она
    # применяется скорером отдельно и ко всем рукам одинаково.
    for name, _desc in (("trace", "у каждого требования INF/NFR/SEC есть строка трассировки"),
                        ("resolvable", "адрес трассы разрешается в документе")):
        cid = f"CT-{task}-{name}"
        # Пути — через env (см. комментарий в runners/pvlib.py): слепок
        # коммитится в публичный репозиторий и не должен содержать домашних
        # путей. Переменные выставляет pvlib при импорте, их наследует и
        # агент, который сам дергает гейт.
        # Пути только относительные: проверяльщик лежит в самой ячейке
        # (.contour/), поэтому рульсет переносим и не содержит ни домашних
        # путей, ни переменных окружения. Внутренних кавычек нет — значение
        # подставляется в YAML-строку в двойных кавычках.
        cmd = (f"python3 .contour/gate_extra.py --check {name} --repo . "
               f"--meta .contour/task_meta.json")
        extra += [
            f"  - id: {cid}",
            f"    name: contour-{name}",
            "    type: command_succeeds",
            f'    command: "{cmd}"',
            "    severity: high",
            "",
        ]
    ruleset = "\n".join(header) + base.rstrip() + "\n" + "\n".join(extra)
    return ruleset


# ---------------------------------------------------------------------------


def main():
    CONTOUR.mkdir(parents=True, exist_ok=True)
    tasks = sys.argv[1:] or pvlib.list_tasks()
    written = {"acceptance": 0, "defects": 0, "quiz": 0, "ruleset": 0}
    for task in tasks:
        d = CONTOUR / task
        d.mkdir(parents=True, exist_ok=True)
        items = [("ACCEPTANCE.md", gen_acceptance(task), "acceptance"),
                 ("DEFECTS.yaml", defects_yaml(gen_defects(task)), "defects"),
                 ("handoff_quiz.yaml", quiz_yaml(gen_quiz(task)), "quiz"),
                 ("CONSTRAINTS.contour.yaml", gen_ruleset(task), "ruleset")]
        for fname, content, key in items:
            p = d / fname
            if p.is_file() and os_environ_frozen():
                continue
            p.write_text(content, encoding="utf-8")
            written[key] += 1
    print(json.dumps({"задач": len(tasks), "записано": written},
                     ensure_ascii=False))


def os_environ_frozen():
    import os
    return os.environ.get("CONTOUR_KEEP") == "1"


if __name__ == "__main__":
    main()
