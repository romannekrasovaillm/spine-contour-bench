#!/usr/bin/env python3
"""stage2_pending.py — сколько ячеек стадии 2 ещё не сдано.

Зачем отдельный счётчик. Обёртка прогона считала «осталось» по ОТСУТСТВИЮ
`meta2.json`, без фильтра по модели: dsf-цикл считал glm-ячейки, поэтому
казался вечным, а уже сданные, но с ошибкой ячейки — сданными. Здесь
остатком считается ячейка, у которой нет meta2.json ИЛИ он с ошибкой, и
которая вообще имеет материал стадии 1 (иначе она неучастна, а не «ждёт»).

Группы:
  a — glm без theseus (основной контраст: spine-arch, claude-spine,
      claude-plain);
  b — theseus-spine, обе модели;
  all — всё, что ещё не сдано.

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/stage2_pending.py a [--list]
Только stdlib.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

# Стадия 2 идёт только на этих руках; остальные руки в прогоне есть, но
# стадии 2 у них нет — без этого фильтра счётчик показывал 62 «ждущих»
# вместо 26 (в них попадали руки, которых стадия 2 не касается).
STAGE2_ARMS = ("spine-arch", "claude-spine", "theseus-spine", "claude-plain")


def _arm(n):
    return n.split("__")[1]


GROUPS = {
    "a": lambda n: "__glm__" in n and _arm(n) in STAGE2_ARMS
    and _arm(n) != "theseus-spine",
    "b": lambda n: _arm(n) == "theseus-spine",
    "all": lambda n: _arm(n) in STAGE2_ARMS,
}


def has_stage1_material(c):
    return ((c / "answer.md").is_file()
            or (c / "work" / "answer.md").is_file()
            or (c / "answer.v1.md").is_file())


def pending(group):
    out, no_material = [], []
    for c in sorted(pvlib.CELLS_DIR.iterdir()):
        if not c.is_dir() or not GROUPS[group](c.name):
            continue
        m2 = c / "meta2.json"
        if m2.is_file() and not json.loads(
                m2.read_text(encoding="utf-8")).get("error"):
            continue
        (out if has_stage1_material(c) else no_material).append(c.name)
    return out, no_material


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", choices=sorted(GROUPS))
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    out, no_mat = pending(a.group)
    if a.list:
        print("\n".join(out))
        if no_mat:
            print("-- без материала стадии 1 (неучастны):", file=sys.stderr)
            print("\n".join(no_mat), file=sys.stderr)
    else:
        print(len(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
