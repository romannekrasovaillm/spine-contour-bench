#!/usr/bin/env python3
"""prepare_stage2_rerun.py — подготовка чистой стадии 2 для перезапуска.

ДВА РЕЖИМА, оба нужны, потому что заслон (`chmod 000` на каталоги дизайна)
блокирует сам раннер: инъекцию делает `defects/inject.py`, и под заслоном
она падает с PermissionError. Поэтому инъекция вынесена в отдельное окно,
когда заслон снят, а агенты ещё не запущены.

  --reinject   пересоздать v1.1 ИЗ НЕТРОНУТОГО v1.

    Зачем. Ячейку, у которой преемник не записал `answer.md`, раннер считал
    сбоем и запускал заново. Каждый заход правил `WORK-IN-PROGRESS.md`
    дальше, и следующий преемник получал уже ЧАСТИЧНО ИСПРАВЛЕННЫЙ документ.
    Это смещает метрику `caught` вверх: дефект, снятый прошлым заходом, в
    финале отсутствует и детектор считает его пойманным. Замер становится
    недействительным, поэтому ячейка возвращается ровно в то состояние,
    которое определено протоколом: v1.1, полученный однократной
    детерминированной порчей v1.

    Детерминированность не декларируется, а проверяется: v1.1 пересобирается
    копией production-кода в временном каталоге, и состав инъекции
    сверяется с уже лежащим `injection.json` (коды тезисов, контрольного
    класса и структурных дефектов). Расхождение — отказ, а не «похоже,
    сойдёт».

  --inject     проронить инъекцию обычным путём (для ячеек, где её ещё нет).

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/prepare_stage2_rerun.py --reinject
  PVBENCH_RUNS=<каталог> python3 runners/prepare_stage2_rerun.py --inject
Только stdlib.
"""
import argparse
import fnmatch
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

STAGE2_ARMS = ("spine-arch", "claude-spine", "theseus-spine", "claude-plain")


def _load_inject():
    spec = importlib.util.spec_from_file_location(
        "inject", str(pvlib.BASE / "defects" / "inject.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest_key(man):
    """Сопоставимая часть манифеста: состав инъекции, без путей и счётчиков."""
    return {
        "fact": [x["id"] for x in man.get("fact", [])],
        "control": [x["id"] for x in man.get("control", [])],
        "rule": [(x["id"], x["op"], bool(x.get("skipped")))
                 for x in man.get("rule", [])],
    }


def reinject(cell, inj):
    """Пересобрать v1.1 из v1 в временном каталоге и проверить совпадение."""
    task = cell.name.split("__")[0]
    v1p = cell / "answer.v1.md"
    if not v1p.is_file():
        return {"cell": cell.name, "skip": "нет answer.v1.md"}
    old = cell / "injection.json"
    if not old.is_file():
        return {"cell": cell.name, "skip": "нет injection.json"}
    old_key = _manifest_key(json.loads(old.read_text(encoding="utf-8")))

    tmp = Path(tempfile.mkdtemp(prefix="reinj-"))
    try:
        (tmp / "work").mkdir()
        shutil.copy2(v1p, tmp / "work" / "answer.md")
        res = inj.inject(tmp, task)
        if not res.get("ok"):
            return {"cell": cell.name, "error": res.get("reason")}
        new_key = _manifest_key(
            json.loads((tmp / "injection.json").read_text(encoding="utf-8")))
        if new_key != old_key:
            return {"cell": cell.name, "error": "состав инъекции разошёлся",
                    "old": old_key, "new": new_key}
        wip = (tmp / "work" / "WORK-IN-PROGRESS.md").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    (cell / "work" / "WORK-IN-PROGRESS.md").write_text(wip, encoding="utf-8")
    # Преемник обязан написать финал сам: убираем всё, что он мог бы
    # принять за готовый ответ.
    for junk in ("answer.md", "answer.stage2.md"):
        p = cell / "work" / junk
        if p.is_file():
            p.unlink()
    for junk in ("meta2.json",):
        p = cell / junk
        if p.is_file():
            p.unlink()
    return {"cell": cell.name, "ok": True, "wip_bytes": len(wip.encode()),
            "fact": len(old_key["fact"]), "control": len(old_key["control"]),
            "rule": [r[0] for r in old_key["rule"]]}


def inject_fresh(cell, inj):
    task = cell.name.split("__")[0]
    res = inj.inject(cell, task)
    return {"cell": cell.name, **res}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reinject", action="store_true")
    ap.add_argument("--inject", action="store_true")
    ap.add_argument("--only-glob", default="*")
    ap.add_argument("--cells", default="",
                    help="явный список ячеек (можно с glob-шаблонами): "
                         "нужен для перегона уличенных ячеек, которые в "
                         "общий шаблон не собираются")
    a = ap.parse_args()
    if not (a.reinject or a.inject):
        print("нужен --reinject или --inject")
        return 1
    inj = _load_inject()
    pats = a.cells.split()
    out = []
    for c in sorted(pvlib.CELLS_DIR.iterdir()):
        if not c.is_dir():
            continue
        hit = any(fnmatch.fnmatch(c.name, p) for p in pats) if pats \
            else c.match(a.only_glob)
        if not hit:
            continue
        arm = c.name.split("__")[1]
        if arm not in STAGE2_ARMS:
            continue
        # Ячейка с УСПЕШНЫМ meta2.json закрыта; с ошибочным — идёт на повтор
        # (иначе повторные заходы, ради которых всё и затевается, отсеялись бы).
        m2 = c / "meta2.json"
        if m2.is_file() and not json.loads(
                m2.read_text(encoding="utf-8")).get("error"):
            continue
        if a.reinject:
            if not (c / "injection.json").is_file():
                continue
            r = reinject(c, inj)
        else:
            if (c / "injection.json").is_file():
                continue
            if not (c / "work" / "answer.md").is_file():
                continue
            r = inject_fresh(c, inj)
        out.append(r)
        tag = r.get("error") or r.get("skip") or "ok"
        print(f"  {c.name:48s} {tag}")
    ok = sum(1 for r in out if r.get("ok"))
    bad = [r for r in out if r.get("error")]
    print(f"\nобработано: {len(out)}; успешно: {ok}; отказов: {len(bad)}")
    if bad:
        for r in bad:
            print("  !!", json.dumps(r, ensure_ascii=False)[:200])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
