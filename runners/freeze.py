#!/usr/bin/env python3
"""freeze.py — машинный слепок sha256 всех входов прогона.

Заморозка ДО первой генерации: после неё менять задачи, контурные артефакты,
рульсеты, дефектный корпус, промпты и раннеры нельзя — правка означает новый
прогон, а расхождение видно сверкой слепка.

Использование:
  python3 runners/freeze.py            # сверить с prereg.lock.json
  python3 runners/freeze.py --write    # записать (только до первого прогона)
Только stdlib.
"""
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOCK = BASE / "prereg.lock.json"
GLOBS = ["tasks/**/*.md", "contour/**/*", "spine/**/*", "customization/*.md",
         "runners/*.py", "defects/*.py", "runners/mech_overrides.yaml",
         "PREREGISTRATION.md", "AEF-1-CHECKLIST.md", "COI-POLICY.md"]


def digest():
    out = {}
    for g in GLOBS:
        for p in sorted(BASE.glob(g)):
            if p.is_file() and "__pycache__" not in str(p):
                out[str(p.relative_to(BASE))] = hashlib.sha256(
                    p.read_bytes()).hexdigest()
    return out


def main():
    cur = digest()
    if "--write" in sys.argv:
        LOCK.write_text(json.dumps(
            {"files": cur, "count": len(cur)}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"заморожено файлов: {len(cur)} → {LOCK.name}")
        return 0
    if not LOCK.is_file():
        print("prereg.lock.json отсутствует — заморозка не сделана")
        return 1
    old = json.loads(LOCK.read_text(encoding="utf-8")).get("files", {})
    diff = [k for k in set(old) | set(cur) if old.get(k) != cur.get(k)]
    for k in sorted(diff):
        st = ("изменён" if k in old and k in cur else
              "удалён" if k in old else "добавлен")
        print(f"  {st}: {k}")
    print(f"файлов в слепке: {len(old)}; расхождений: {len(diff)}")
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
