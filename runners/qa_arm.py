#!/usr/bin/env python3
"""qa_arm.py — проверка ячеек руки перед судейством (D22).

Помечает то, что нельзя судить как есть:
  * no_answer   — ответа нет (ячейка не догналась);
  * raw_stream  — в answer.md попал неразобранный поток stream-json;
  * missing     — мех-контроль не нашёл обязательных артефактов D1–Dn:
                  обычно это обрыв посреди потока, когда начало документа
                  не выжило и восстановить его извлечением нельзя.

Дополняет runners/audit_extraction.py, который сверяет answer.md с журналом
сессии (классы limit_split и not_final_message). Вместе они закрывают все
известные способы порчи ответа во время прогона.

Печатает список ячеек к перегону строкой PVBENCH_CELLS=«…».

Использование:
  PVBENCH_RUNS=<каталог прогона> python3 runners/qa_arm.py [--glob '*claude*glm*']
Только stdlib.
"""
import argparse
import glob
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="*claude*glm*",
                    help="маска имени ячейки (по умолчанию — glm-рука Claude Code)")
    ap.add_argument("--cells-dir", default=str(pvlib.CELLS_DIR))
    args = ap.parse_args()

    bad, ok = [], 0
    for d in sorted(glob.glob(os.path.join(args.cells_dir, args.glob))):
        name = os.path.basename(d)
        task = name.split("__")[0]
        ap_ = os.path.join(d, "answer.md")
        if not os.path.exists(ap_):
            bad.append((name, "no_answer", ""))
            continue
        text = open(ap_, encoding="utf-8", errors="replace").read()
        if text.lstrip().startswith('{"type"') or '"type":"assistant"' in text[:400]:
            bad.append((name, "raw_stream", f"{len(text.encode())} Б"))
            continue
        mech, _ = pvlib.load_mech(task)
        miss = [e["id"] for e in mech["deliverables"]
                if not re.search(e["pattern"], text)]
        if miss:
            bad.append((name, "missing", f"{len(text.encode())} Б, нет {miss}"))
        else:
            ok += 1
    for name, kind, note in bad:
        print(f"  {kind:<11} {name:<45} {note}")
    print(f"\nчистых: {ok}; к перегону: {len(bad)}")
    if bad:
        print("PVBENCH_CELLS=\"" + " ".join(n for n, _, _ in bad) + "\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
