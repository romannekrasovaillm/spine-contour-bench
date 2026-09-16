#!/usr/bin/env python3
"""stage2_resume.py — стадия 2: преемник продолжает работу вместо стадии 1.

Ход для каждой ячейки:

  1. defects/inject.py <cell> --task <TASK>   — порча v1 и подготовка v1.1
     (делается здесь, если injection.json ещё нет);
  2. преемник запускается тем же харнессом с prompt2.txt; рабочая сессия
     стадии 1 ему недоступна — только каталог work/;
  3. stdout преемника → answer.stage2.md → answer.md (финал), meta2.json.

Результат работы стадии 1 сохранён в answer.v1.md ещё инъекцией, поэтому
answer.md после стадии 2 — это ФИНАЛ, а v1 не теряется.

Идемпотентно: ячейка с готовым meta2.json без ошибки пропускается.

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/stage2_resume.py [--only-glob '*claude-spine*']
Только stdlib.
"""
import argparse
import fnmatch
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib
import run_matrix as rm
import prepare_cells as pc

CELLS = pvlib.CELLS_DIR
LOGS = pvlib.LOGS_DIR
MIN_BYTES = 500
SUCCESSOR_MODEL = os.environ.get("PVBENCH_SUCCESSOR_MODEL", "same")
_log_lock = threading.Lock()


def run_successor(task, arm, model, cell):
    """Прогон преемника. Возвращает (answer|None, cmd, rc, err)."""
    work = cell / "work"
    prompt = (cell / "prompt2.txt").read_text(encoding="utf-8")
    argv = rm.build_cmd(task, arm, model, prompt, uniq=cell.name + "-s2")
    cmd_str = rm.redact(" ".join(a if a != prompt else "$(cat prompt2.txt)"
                                 for a in argv))
    try:
        proc = subprocess.Popen(argv, cwd=work, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            out, errout = proc.communicate(timeout=rm.TIMEOUT_AGENTIC)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            try:
                out, errout = proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                for f in (proc.stdout, proc.stderr):
                    try:
                        f.close()
                    except Exception:
                        pass
                proc.kill()
                return None, cmd_str, 124, "timeout (потомки не отвечают)"
            return None, cmd_str, 124, f"timeout {rm.TIMEOUT_AGENTIC}s"
    except Exception as e:  # noqa: BLE001
        return None, cmd_str, 1, f"{type(e).__name__}: {str(e)[:160]}"
    if proc.returncode != 0:
        return None, cmd_str, proc.returncode, (errout or out)[-300:]
    out = out or ""
    if arm.startswith("theseus"):
        answer = rm.extract_theseus(work, out)
    elif arm.startswith("claude") and os.environ.get("PVBENCH_CLAUDE_STREAM"):
        answer = rm.extract_claude_stream(out) or out.strip()
    else:
        answer = out.strip()
    if len(answer.encode("utf-8")) < MIN_BYTES:
        return None, cmd_str, 0, f"ответ слишком короткий ({len(answer.encode())} б)"
    return answer, cmd_str, 0, None


def process(cell_name):
    m = re.match(r"^(.+?)__(.+)__(dsf|glm)__r(\d+)$", cell_name)
    task, arm, model = m.group(1), m.group(2), m.group(3)
    cell = CELLS / cell_name

    # 0. инъекция (если ещё не сделана)
    if not (cell / "injection.json").is_file():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "inject", str(pvlib.BASE / "defects" / "inject.py"))
        inj = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inj)
        res = inj.inject(cell, task)
        if not res.get("ok"):
            return {"cell": cell_name, "error": res.get("reason"),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    work = cell / "work"
    t0 = time.time()
    answer, cmd_str, rc, err = None, "", 1, None
    sem = rm.SEM[rm.channel(arm, model)]
    resume = work / "RESUME.md"
    before = resume.stat().st_mtime if resume.is_file() else None
    for attempt in range(rm.RETRIES + 1):
        with sem:
            answer, cmd_str, rc, err = run_successor(task, arm, model, cell)
        if answer is not None:
            break
        time.sleep(rm.RETRY_SLEEP * (attempt + 1))
    dt = time.time() - t0

    src = "file"
    wf = work / "answer.md"
    if answer is not None:
        if wf.is_file() and wf.stat().st_size >= MIN_BYTES:
            answer = wf.read_text(encoding="utf-8", errors="replace")
        else:
            src = "stdout"
            wf.write_text(answer, encoding="utf-8")
        (cell / "answer.stage2.md").write_text(answer, encoding="utf-8")
        # финал: answer.md на уровне ячейки = работа преемника
        # (версия стадии 1 уже сохранена как answer.v1.md)
        (cell / "answer.md").write_text(answer, encoding="utf-8")

    meta = {"cell": cell_name, "task": task, "arm": arm, "model": model,
            "stage": 2, "cmd": cmd_str, "secs": round(dt, 1), "exit_code": rc,
            "bytes": len(answer.encode("utf-8")) if answer else 0,
            "deliverable_source": src,
            "resume_md_created": (resume.is_file()
                                  and resume.stat().st_mtime != before),
            "error": err, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (cell / "meta2.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    with _log_lock:
        LOGS.mkdir(exist_ok=True)
        with (LOGS / "stage2.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    status = (f"ok {meta['bytes']}b {meta['secs']}s" if answer
              else f"ERR {str(err)[:90]}")
    resume_note = "RESUME.md:да" if meta["resume_md_created"] else "RESUME.md:НЕТ"
    print(f"{cell_name}: {status}  {resume_note}", flush=True)
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-glob", default="*")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=1,
                    help="сколько преемников гонять параллельно (по умолчанию 1)")
    a = ap.parse_args()
    names = []
    for p in sorted(CELLS.iterdir()):
        if not p.is_dir() or not fnmatch.fnmatch(p.name, a.only_glob):
            continue
        m = re.match(r"^(.+?)__(.+)__(dsf|glm)__r(\d+)$", p.name)
        if not m or m.group(2) not in pc.STAGE2_ARMS:
            continue
        # Стадия 1 сдана, если сдан её артефакт: либо копия на уровне ячейки
        # (её пишет run_matrix), либо сохранённая версия v1 (её пишет инъекция
        # и удаляет work/answer.md, чтобы преемник писал финал сам). Требовать
        # только answer.v1.md нельзя: он появляется лишь ПОСЛЕ инъекции,
        # которая делается внутри process() — получался замкнутый круг.
        if not ((p / "answer.md").is_file() or (p / "answer.v1.md").is_file()):
            continue
        meta2 = p / "meta2.json"
        if meta2.is_file():
            try:
                if not json.loads(meta2.read_text(encoding="utf-8")).get("error"):
                    continue              # уже сделано
            except ValueError:
                pass
        names.append(p.name)
    if a.limit:
        names = names[:a.limit]
    print(f"ячеек стадии 2 к прогону: {len(names)}; параллельно: {a.jobs}",
          flush=True)

    def safe(n):
        try:
            process(n)
        except Exception as e:  # noqa: BLE001
            print(f"{n}: СБОЙ ОРКЕСТРАЦИИ {type(e).__name__}: {e}", flush=True)

    if a.jobs <= 1:
        for n in names:
            safe(n)
        return 0
    # Параллельный прогон. До 20:00 16.09 стадия 2 шла по одной ячейке —
    # это оказалось узким местом: glm-ячейки берут 15–30 мин, и хвост из 38
    # ячеек растягивался на ~12 часов. Параллелизм меняет только пропускную
    # способность: промпты, модели, бюджет попытки и ретраи те же, а каналы
    # по-прежнему ограничены семафорами rm.SEM (claude ≤ PVBENCH_CLAUDE_PAR,
    # glm ≤ PVBENCH_GLM_PAR) — они и защищают от 429 (D22 первого бенчмарка).
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(safe, names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
