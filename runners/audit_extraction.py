#!/usr/bin/env python3
"""audit_extraction.py — аудит извлечения ответов Claude Code (D22).

Проверяет, что cells/<cell>/answer.md — это ровно итоговый документ сессии,
а не её фрагмент и не служебное подтверждение. Источник сверки — журналы
сессий Claude Code: ~/.claude/projects/<slug>/*.jsonl, где slug — каталог
work/ ячейки, в котором запускался харнесс (не-буквенно-цифровые символы
заменены на «-»).

Три класса дефектов (все встречены в прогоне 09–13.09.2026):
  1. `limit_split` — сессия упиралась в лимит вывода токенов, CLI продолжал
     генерацию отдельным сообщением, а раннер сохранял только последнее:
     в answer.md не хватает начала документа (пример:
     CMP-ARCH-001__claude-plain__glm__r1, потеряны D1–D9).
  2. `not_final_message` — answer.md не совпадает с итоговым сообщением
     сессии (пример: PVH-ARCH-001__claude-arch__dsf__r2 — агент записал
     документ прямо в путь ячейки, раннер затёр его stdout-подтверждением).
  3. `no_transcript` — сессии нет вовсе (ячейка не запускалась).
Плюс мягкий флаг `multi_chunk` — в сессии несколько крупных чанков, но
answer.md совпал с итоговым сообщением (обычно это добровольная переписка
документа под лимит слов, а не потеря): требует взгляда глазами, а не
автоматического брака.

Использование:
  PVBENCH_RUNS=<каталог прогона> python3 runners/audit_extraction.py \
      [--only "claude-arch claude-plain"] [--json отчёт.json]
Код возврата: 0 — дефектов нет, 1 — есть (перечислены в stdout).
Только stdlib.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR",
                               str(Path.home() / ".claude" / "projects")))
LIMIT_MARKER = "Output token limit hit"
MIN_DOC_BYTES = 2000


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def parse_session(p):
    """Список сообщений сессии: [{role, text, ts}] (только текстовые блоки)."""
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") or {}
        role = m.get("role")
        if role not in ("assistant", "user"):
            continue
        c = m.get("content")
        if isinstance(c, str):
            txt = c
        elif isinstance(c, list):
            txt = "".join(b.get("text", "") for b in c
                          if isinstance(b, dict) and b.get("type") == "text")
        else:
            txt = ""
        out.append({"role": role, "text": txt, "ts": d.get("timestamp", "")})
    return out


def audit_cell(cell):
    """Возвращает dict с полями cell/answer_bytes/verdict/note."""
    rec = {"cell": cell.name, "answer_bytes": None, "verdict": "ok", "note": "",
           "sessions": 0, "lost_chars": 0}
    ap = cell / "answer.md"
    if not ap.is_file():
        rec.update(verdict="no_answer", note="answer.md отсутствует")
        return rec
    answer = ap.read_text(encoding="utf-8", errors="replace").strip()
    rec["answer_bytes"] = len(answer.encode("utf-8"))
    # страховка от фолбэка в сырой stdout: в answer.md не должно быть потока
    if answer.startswith('{"type"') or '"type":"assistant"' in answer[:400]:
        rec.update(verdict="raw_stream",
                   note="в answer.md попал неразобранный stream-json")
        return rec
    sd = PROJECTS / slug(cell / "work")
    trs = sorted(sd.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    rec["sessions"] = len(trs)
    if not trs:
        rec.update(verdict="no_transcript",
                   note=f"нет журналов сессии в {sd}")
        return rec
    matched = False
    for p in trs:
        msgs = parse_session(p)
        asst = [m for m in msgs if m["role"] == "assistant" and m["text"].strip()]
        if not asst:
            continue
        last = asst[-1]["text"].strip()
        if not (last == answer or answer[:300] == last[:300]):
            continue
        matched = True
        # потерянные крупные куски: предыдущие assistant-сообщения сессии
        lost = sum(len(m["text"]) for m in asst[:-1] if len(m["text"]) > MIN_DOC_BYTES)
        rec["lost_chars"] = lost
        # был ли в сессии маркер лимита вывода
        if any(LIMIT_MARKER in m["text"] for m in msgs if m["role"] == "user"):
            rec.update(verdict="limit_split",
                       note=f"сессия упиралась в лимит вывода; "
                            f"потеряно ~{lost} симв. предыдущих чанков")
        elif lost:
            rec.update(verdict="multi_chunk",
                       note=f"в сессии несколько крупных чанков "
                            f"(~{lost} симв. не в answer.md)")
        break
    if not matched:
        # Склейка чанков (D22) даёт ответ длиннее итогового сообщения сессии:
        # итоговое сообщение входит в него суффиксом. Это не дефект.
        spliced = False
        for p in trs:
            asst = [m for m in parse_session(p)
                    if m["role"] == "assistant" and m["text"].strip()]
            if asst and answer.endswith(asst[-1]["text"].strip()):
                spliced = True
                break
        if spliced:
            rec.update(verdict="spliced",
                       note="ответ длиннее итогового сообщения — склейка "
                            "чанков после лимита вывода (ожидаемо, D22)")
        else:
            rec.update(verdict="not_final_message",
                       note="answer.md не совпадает с итоговым сообщением сессии")
    elif rec["answer_bytes"] < MIN_DOC_BYTES and rec["verdict"] == "ok":
        rec.update(verdict="too_short",
                   note=f"ответ короче {MIN_DOC_BYTES} Б и совпал с итоговым "
                        f"сообщением — похоже на служебное подтверждение")
    return rec


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--only", default="claude",
                     help="условия через пробел (подстрока имени ячейки)")
    ap_.add_argument("--json", default=None, help="куда сохранить отчёт")
    args = ap_.parse_args()
    conds = args.only.split()
    cells = [c for c in sorted(pvlib.CELLS_DIR.iterdir()) if c.is_dir()
             and any(f"__{x}" in c.name or c.name.startswith(x) for x in conds)]
    recs = [audit_cell(c) for c in cells]
    bad = [r for r in recs if r["verdict"] not in ("ok",)]
    for r in bad:
        print(f"  {r['verdict']:<17} {r['cell']:<45} "
              f"B={r['answer_bytes']} {r['note']}")
    print(f"\nпроверено ячеек: {len(recs)}; с замечаниями: {len(bad)}"
          f" (из них без ответа: {sum(1 for r in bad if r['verdict']=='no_answer')})")
    if args.json:
        Path(args.json).write_text(
            json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"отчёт: {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
