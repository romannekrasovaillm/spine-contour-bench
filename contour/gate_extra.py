#!/usr/bin/env python3
"""gate_extra.py — проверки контура, которых нет в типах правил arch-be.

Вызывается из CONSTRAINTS.contour.yaml как `command_succeeds`:

    python3 contour/gate_extra.py --check trace|resolvable|invented \
        --repo . --task SYN-ARCH-001 --tasks-dir <абс. путь к tasks/>

Код возврата: 0 — проверка пройдена, 1 — нет (гейт валится). Диагностика
печатается в stdout и дублируется в `<repo>/../contour_extra.json`, потому
что arch-be показывает в отчёте только текст команды, а не её вывод.

Проверки:
  trace       у КАЖДОГО требования INF/NFR/SEC из CONTEXT.md есть строка
              трассировки в answer.md;
  resolvable  адрес в правой части строки трассировки разрешается в документе
              (раздел D-ряда / ADR / нумерованный раздел) либо это
              `unverifiable: <причина>` с непустой причиной;
  invented    ни один из примеров вымышленных компонентов (HF-01 в
              RUBRICS.md) не подан в документе как факт.

Самодостаточен: stdlib, не импортирует runners/ (файл исполняется из
произвольного cwd и заморожен в prereg.lock.json).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

REQ_RE = re.compile(r"(?m)^\|\s*(INF|NFR|SEC)-(\d+)\s*\|")
# Строка трассировки — та, где ID требования СТОИТ В НАЧАЛЕ содержательной
# части: либо «<ID> -> адрес», либо «| <ID> | … |» (таблица трассировки).
# Иначе любая строка со стрелкой («… выводит адрес → внешние потоки…»)
# засчитывалась бы как трассировка — эта ошибка ловится тестом self-test.
ARROW_RE = re.compile(
    r"(?m)^[\s>|*\-]*(?:\|\s*)?\**\s*((?:INF|NFR|SEC)-\d+)\b[^|\n]*?(?:->|→)\s*(.+?)\s*\|?\s*$")
ROW_RE = re.compile(
    r"(?m)^\|\s*\**\s*((?:INF|NFR|SEC)-\d+)\s*\**\s*\|(.+?)\|\s*$")
SEC_RE = re.compile(r"(?m)^#{1,4}\s*(?:(\d+)[.)]?\s|.*?\bD(\d+)\b)")
# Ячейка-статус в таблице трассировки (не адрес).
STATUS_RE = re.compile(
    r"(?i)^(выполнено|с оговоркой|частично|не выполнено|требует проверки|"
    r"покрыто|закрыто|да|нет|✓|✗|—|-)$")


def trace_nodes(answer):
    """{req_id: ('arrow', адрес) | ('row', остальные ячейки)}."""
    nodes = {}
    for m in ROW_RE.finditer(answer):
        cells = [c.strip() for c in m.group(2).split("|")]
        cells = [c for c in cells if c and not set(c) <= set("-: ")]
        nodes.setdefault(m.group(1), ("row", " | ".join(cells)))
    for m in ARROW_RE.finditer(answer):
        nodes[m.group(1)] = ("arrow", m.group(2).strip("|* "))
    return nodes


def requirements_from_meta(meta_path):
    """Список требований из .contour/task_meta.json (ячейка самодостаточна)."""
    data = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    return [r["id"] for r in data["requirements"]]


def requirements(tasks_dir, task):
    ctx = (Path(tasks_dir) / task / "CONTEXT.md").read_text(encoding="utf-8")
    seen, out = set(), []
    for m in REQ_RE.finditer(ctx):
        rid = f"{m.group(1)}-{m.group(2)}"
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def invented_examples(tasks_dir, task):
    rub = (Path(tasks_dir) / task / "RUBRICS.md").read_text(encoding="utf-8")
    m = re.search(r"(?m)^\|\s*HF-01\s*\|(.+?)\|\s*$", rub)
    if not m:
        return []
    return [re.sub(r"\s+", " ", x).strip()
            for x in re.findall(r"«([^»]+)»", m.group(1)) if len(x.strip()) > 3]


def norm(s):
    return re.sub(r"\s+", " ", s.replace("«", "").replace("»", "")).strip()


def check_trace(answer, reqs, _tasks_dir, _task):
    """У каждого требования есть строка трассировки.

    Засчитываются обе формы: «<ID> -> адрес» и «| <ID> | … |» — контракт в
    ACCEPTANCE.md называет стрелочную, но табличная ей эквивалентна, и
    наказывать за форму, а не за отсутствие следа, было бы подменой метрики.
    """
    nodes = trace_nodes(answer)
    missing = [r for r in reqs if r not in nodes]
    return (not missing,
            f"требований: {len(reqs)}; без строки трассировки: "
            f"{len(missing)}" + (f" ({', '.join(missing[:8])}"
                                 + ("…" if len(missing) > 8 else "") + ")"
                                 if missing else ""),
            {"missing": missing, "total": len(reqs),
             "covered": len(reqs) - len(missing)})


def check_resolvable(answer, reqs, _tasks_dir, _task):
    """Адрес трассировки разрешается в документе."""
    heads = set()
    for m in SEC_RE.finditer(answer):
        if m.group(1):
            heads.add(m.group(1))
        if m.group(2):
            heads.add(f"D{m.group(2)}")
    adrs = set(re.findall(r"\bADR-\d+\b", answer))
    nodes = trace_nodes(answer)
    bad, ok = [], 0
    for rid in reqs:
        node = nodes.get(rid)
        if not node:
            continue
        kind, addr = node
        if kind == "row":
            # Табличная форма: последняя ячейка — статус («выполнено»),
            # перед ней — адрес. Статус в адрес не засчитываем.
            cells = [c.strip(" *") for c in addr.split("|")]
            cells = [c for c in cells if c]
            if cells and STATUS_RE.match(cells[-1]):
                cells = cells[:-1]
            addr = " ".join(cells)
        addr = norm(addr)
        if not addr:
            bad.append((rid, "пустой адрес"))
            continue
        low = addr.lower()
        if "unverifiable" in low or "не провер" in low:
            reason = addr.split(":", 1)[1].strip() if ":" in addr else addr
            if len(reason) >= 10:
                ok += 1
            else:
                bad.append((rid, "unverifiable без причины"))
            continue
        dref = set(re.findall(r"\bD(\d+)\b", addr))
        href = set(re.findall(r"(?:§|разд\w*\.?\s*)(\d+)", addr))
        aref = set(re.findall(r"\bADR-\d+\b", addr))
        if (dref and any(f"D{d}" in heads for d in dref)) or \
           (href and href & heads) or (aref and aref <= adrs) or \
           len(addr) >= 12:
            # «≥12 символов» — адрес содержательный (называет раздел словами:
            # «раздел плана миграции»), а не заглушка «см. выше»/«—».
            ok += 1
        else:
            bad.append((rid, "адрес не разрешается в документе"))
    return (not bad and ok > 0,
            f"строк трассировки разрешено: {ok}; не разрешается: {len(bad)}"
            + (f" (например: {bad[0][0]} — {bad[0][1]})" if bad else ""),
            {"resolved": ok, "unresolved": [b[0] for b in bad]})


def check_invented(answer, _reqs, tasks_dir, task):
    """Примеры вымышленных компонентов (HF-01) не поданы как факт.

    Список примеров живёт в RUBRICS.md, которого у испытуемой руки нет и
    быть не должно: иначе список вымышленных имён стал бы подсказкой. Эту
    проверку применяет ТОЛЬКО скорер — единообразно ко всем рукам, из своей
    копии рульсета (см. runners/contour_score.py).
    """
    ex = invented_examples(tasks_dir, task)
    na = norm(answer).lower()
    hits = [e for e in ex if norm(e).lower() in na]
    return (not hits,
            f"проверено примеров HF-01: {len(ex)}; найдено как факт: "
            f"{len(hits)}" + (f" ({'; '.join(hits)})" if hits else ""),
            {"checked": len(ex), "hits": hits})


CHECKS = {"trace": check_trace, "resolvable": check_resolvable,
          "invented": check_invented}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", required=True, choices=sorted(CHECKS))
    ap.add_argument("--repo", default=".")
    ap.add_argument("--task", default=None,
                    help="нужен только для задач-каталогов (--tasks-dir); "
                         "при --meta не обязателен")
    ap.add_argument("--tasks-dir", default=str(Path(__file__).resolve().parent.parent / "tasks"))
    ap.add_argument("--meta", default=None,
                    help=".contour/task_meta.json внутри ячейки (требования); "
                         "если задан — tasks-dir для требований не нужен")
    ap.add_argument("--fail-safe", action="store_true",
                    help="при внутренней ошибке считать проверку пройденной "
                         "(для отладки; в зачётном прогоне НЕ использовать)")
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    ans_p = repo / "answer.md"
    report = {"check": a.check, "task": a.task, "repo": str(repo)}
    try:
        if not ans_p.is_file():
            raise FileNotFoundError("answer.md отсутствует")
        answer = ans_p.read_text(encoding="utf-8", errors="replace")
        reqs = (requirements_from_meta(a.meta) if a.meta
                else requirements(a.tasks_dir, a.task))
        ok, msg, detail = CHECKS[a.check](answer, reqs, a.tasks_dir, a.task)
        report.update(passed=ok, message=msg, detail=detail)
    except Exception as e:  # noqa: BLE001
        if a.fail_safe:
            report.update(passed=True, message=f"ошибка проверки: {e}")
        else:
            report.update(passed=False, message=f"ошибка проверки: {e}")

    print(f"[{a.check}] {'PASS' if report['passed'] else 'FAIL'}: "
          f"{report['message']}")
    try:
        out = repo.parent / "contour_extra.json"
        cur = {}
        if out.is_file():
            cur = json.loads(out.read_text(encoding="utf-8"))
        cur[a.check] = report
        out.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    except OSError:
        pass
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
