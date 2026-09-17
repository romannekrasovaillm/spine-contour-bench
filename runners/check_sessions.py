#!/usr/bin/env python3
"""check_sessions.py — не вели ли одну ячейку ДВА процесса одновременно.

ЗАЧЕМ. Ночью перегоны и волна идут параллельно, и их маски пересекаются:
волна обходит руки по шаблону, а перегон сбрасывает отдельные ячейки тех же
рук. Если оба процесса возьмут одну ячейку, в ней окажутся два агента, и
замер испортится МОЛЧА — артефакт будет выглядеть нормальным.

Оба раннера идемпотентны (ячейка с готовым артефактом пропускается), но
проверка «кто уже сделал» и запуск агента не атомарны: между ними есть окно.
Гонка уже была возможна (перегон по контраминации сбросил 4 ячейки рук, по
которым тут же шла волна), но не случилась. Это проверка, а не надежда.

КАК ИЩЕТ. По журналам сессий: собирает интервалы [начало, конец] всех
агентских сессий ячейки и сообщает о ПЕРЕСЕКАЮЩИХСЯ. Последовательные сессии
не признак: раннер штатно повторяет попытку после сбоя.

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/check_sessions.py [--since ЭПОХ]
Только stdlib.
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

CLAUDE_PROJ = Path.home() / ".claude" / "projects"


def slug(p):
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(p).resolve()))


def spans(d):
    """[(начало, конец, файл)] по журналу каталога сессий."""
    out = []
    for f in sorted(d.glob("*.jsonl")):
        ts = []
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("timestamp")
            if isinstance(t, str):
                try:
                    ts.append(datetime.datetime.fromisoformat(
                        t.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    pass
        if ts:
            out.append((min(ts), max(ts), f.name))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=float, default=0.0,
                    help="учитывать только сессии, начавшиеся после этой эпохи")
    a = ap.parse_args()
    bad = []
    for c in sorted(pvlib.CELLS_DIR.iterdir()):
        if not c.is_dir():
            continue
        d = CLAUDE_PROJ / slug(c / "work")
        if not d.is_dir():
            continue
        sp = [s for s in spans(d) if s[1] >= a.since]
        for i in range(len(sp) - 1):
            if sp[i][1] > sp[i + 1][0]:
                bad.append((c.name, sp[i], sp[i + 1]))
    print(f"проверено ячеек с журналами: "
          f"{sum(1 for c in pvlib.CELLS_DIR.iterdir() if (CLAUDE_PROJ / slug(c / 'work')).is_dir())}")
    if not bad:
        print("пересекающихся сессий нет: одну ячейку вёл один процесс")
        return 0
    print(f"!! ячеек с ПЕРЕСЕКАЮЩИМИСЯ сессиями: {len(bad)}")
    for name, x, y in bad:
        f = lambda t: datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")
        print(f"  {name}: {f(x[0])}→{f(x[1])} и {f(y[0])}→{f(y[1])} "
              f"({x[2][:20]} / {y[2][:20]})")
    print("\nТакие ячейки недействительны: в них работали два агента.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
