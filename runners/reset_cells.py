#!/usr/bin/env python3
"""reset_cells.py — карантин и сброс уличенных ячеек перед перегоном.

Уличенная аудитом ячейка недействительна ЦЕЛИКОМ, а не только по стадии 2:
рука искала ключ уже на стадии 1 (это видно по её же журналу). Поэтому
перегон идёт с нуля — стадия 1, инъекция, стадия 2.

Что делает:
  1. копирует ячейку целиком в <run>/quarantine/<ячейка>/ — как улику;
     исходный прогон не «поправляется» задним числом, он сохраняется;
  2. убирает из ячейки продукты прогона (ответы, версии, метаданные, журналы
     харнесса) и всё, что рука написала в рабочий каталог, СОХРАНЯЯ входные
     материалы: постановку, контекст, планку, пакет, .contour/ и tools/;
  3. досев входных файлов и промптов НЕ делает — это `prepare_cells.py`
     (он ячейки не стирает, только копирует входные файлы заново), и
     запускать его надо в окне, когда снят заслон на каталоги дизайна.

Как задать ячейки:
  --cells "A B C"          явный список (можно с glob-шаблонами);
  --audit <json>           взять из отчёта аудита те, у кого есть нарушения.

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/reset_cells.py --audit results/audit.json --dry-run
  PVBENCH_RUNS=<каталог> python3 runners/reset_cells.py --audit results/audit.json
Только stdlib.
"""
import argparse
import fnmatch
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

# Входные материалы ячейки: их сбрасывать нельзя.
#   * постановку, контекст, планку и пакет кладёт prepare_cells копированием,
#     и он же пишет их только если их нет;
#   * промпты он ГОТОВИТ, а не копирует, и его сборка читает каталоги дизайна
#     (`tasks/`, `spine/`) — то есть пересборка промпта потребовала бы ещё
#     одного окна без заслона. Хранить исходный промпт не только дешевле:
#     перегон тогда идёт РОВНО с тем же промптом, что и волна, а не с
#     пересобранным (мелкое расхождение в генераторе исказило бы сравнение).
KEEP_NAMES = {
    "TASK.md", "CONTEXT.md", "ACCEPTANCE.md", "ARCHITECTURE-SPINE.md",
    "CONSTRAINTS.yaml", "CONSTRAINTS.contour.yaml", "README.md",
    "gate_extra.py", "task_meta.json", "prompt.txt", "prompt2.txt",
}
KEEP_DIRS = {".contour", "tools"}
# Продукты прогона на уровне ячейки.
DROP_CELL = ("answer.md", "answer.v1.md", "answer.stage2.md", "injection.json",
             "meta.json", "meta2.json", "contour_extra.json")
# Каталоги журналов харнесса: перегон должен иметь свой, отдельный.
DROP_DIRS = (".theseus",)


def plan_cell(cell):
    """Что будет скопировано в карантин и что удалено."""
    victims = []
    for name in DROP_CELL:
        p = cell / name
        if p.exists():
            victims.append(p)
    for d in DROP_DIRS:
        p = cell / d
        if p.exists():
            victims.append(p)
    work = cell / "work"
    if work.is_dir():
        for p in sorted(work.rglob("*")):
            rel = p.relative_to(work).parts
            # входные каталоги сохраняем ВМЕСТЕ с содержимым: иначе `tools/`
            # остался бы пустой оболочкой — файлы внутри удалялись бы, хотя
            # сам каталог в списке сохраняемых
            if rel[0] in KEEP_DIRS:
                continue
            if p.is_dir():
                victims.append(p)
            elif p.name not in KEEP_NAMES:
                victims.append(p)
    return victims


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells", default="")
    ap.add_argument("--audit", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    pats = a.cells.split()
    if a.audit:
        recs = json.loads(Path(a.audit).read_text(encoding="utf-8"))
        pats += [r["cell"] for r in recs if r.get("violations")]
    if not pats:
        print("не задано ни --cells, ни --audit с нарушениями")
        return 1

    cells = [c for c in sorted(pvlib.CELLS_DIR.iterdir())
             if c.is_dir() and any(fnmatch.fnmatch(c.name, p) for p in pats)]
    if not cells:
        print("ячейки не найдены")
        return 1

    qroot = pvlib.RUNS / "quarantine"
    print(f"ячеек к сбросу: {len(cells)}; карантин: {qroot}")
    for c in cells:
        victims = plan_cell(c)
        n_bytes = sum(p.stat().st_size for p in victims
                      if p.is_file())
        n_dirs = sum(1 for p in victims if p.is_dir())
        print(f"  {c.name}: к удалению {len(victims) - n_dirs} файлов "
              f"({n_bytes} б) и {n_dirs} каталогов")
        if not victims and not a.dry_run:
            print("      уже сброшена — карантин не делаю")
            continue
        if a.dry_run:
            for p in victims[:6]:
                print(f"      − {p.relative_to(c)}")
            if len(victims) > 6:
                print(f"      … ещё {len(victims) - 6}")
            continue
        # Карантин версионный: повторный сброс (например, после падения шага)
        # не должен затирать улику ПЕРВОГО сброса — второй раз в ячейке уже
        # пусто, и копия «до» потерялась бы навсегда.
        dest = qroot / c.name
        if dest.exists():
            n = 2
            while (qroot / f"{c.name}-v{n}").exists():
                n += 1
            dest = qroot / f"{c.name}-v{n}"
        dest.mkdir(parents=True, exist_ok=True)
        # Маркер времени сброса: аудит отсекает по нему записи ПРЕЖНИХ попыток.
        # Без него журналы контаминированных сессий (они лежат вне ячейки, в
        # ~/.claude/projects) клеймили бы перегнанную ячейку вечно.
        (c / "RERUN_EPOCH").write_text(str(int(time.time())), encoding="utf-8")
        # 1. улика: копия ячейки целиком. Нечитаемые файлы (на время работы
        # агентов ключ закрыт правами 000) НЕ копируются, а фиксируются в
        # манифесте: копия ключа в карантине создала бы НОВУЮ доступную копию
        # ключа — карантин лежит в каталоге прогона, а из рабочего каталога
        # агента до него три `..`. Содержимое при этом всё равно не прочитать,
        # поэтому честная запись о файле (размер и время) полезнее попытки.
        skipped = []
        for p in sorted(c.rglob("*")):
            rel = p.relative_to(c)
            tgt = dest / rel
            if p.is_dir():
                tgt.mkdir(parents=True, exist_ok=True)
                continue
            try:
                tgt.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, tgt)
            except PermissionError:
                st = p.stat()
                skipped.append({"path": str(rel), "bytes": st.st_size,
                                "mtime": int(st.st_mtime)})
        if skipped:
            (dest / "QUARANTINE-NOTE.json").write_text(
                json.dumps({"cell": c.name,
                            "note": "нечитаемые файлы (заслон ключа) в "
                                    "карантин не копировались — копия ключа "
                                    "создала бы новую доступную копию",
                            "skipped": skipped},
                           ensure_ascii=False, indent=1), encoding="utf-8")
        # 2. сброс продуктов
        for p in sorted(victims, key=lambda x: -len(x.parts)):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
    if a.dry_run:
        print("\nэто план; для сброса уберите --dry-run")
    else:
        print(f"\nсброшено ячеек: {len(cells)}; улики в {qroot}")
        print("дальше: стадия 1 под заслоном (PVBENCH_CELLS) → окно инъекции "
              "→ стадия 2. Промпты и входные материалы сброс сохраняет, "
              "поэтому окно без заслона нужно только под инъекцию")
    return 0


if __name__ == "__main__":
    sys.exit(main())
