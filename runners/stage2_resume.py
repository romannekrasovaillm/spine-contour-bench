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
    wip = work / "WORK-IN-PROGRESS.md"
    wip_before = wip.stat().st_mtime if wip.is_file() else None
    for attempt in range(rm.RETRIES + 1):
        with sem:
            answer, cmd_str, rc, err = run_successor(task, arm, model, cell)
        if answer is not None:
            break
        # Рука, работающая в полученном документе, не создаёт answer.md —
        # без этой проверки раннер считал бы её сбойной и гонял все повторы
        # подряд (для theseus это ~3 × 40 мин на ячейку при живой работе).
        if wip_before is not None and wip.is_file() \
                and wip.stat().st_mtime != wip_before:
            break
        time.sleep(rm.RETRY_SLEEP * (attempt + 1))
    dt = time.time() - t0

    src = "file"
    wf = work / "answer.md"
    wip_modified = None
    if answer is not None:
        if wf.is_file() and wf.stat().st_size >= MIN_BYTES:
            answer = wf.read_text(encoding="utf-8", errors="replace")
        else:
            src = "stdout"
            wf.write_text(answer, encoding="utf-8")
    elif (work / "WORK-IN-PROGRESS.md").is_file() \
            and (work / "WORK-IN-PROGRESS.md").stat().st_size >= MIN_BYTES:
        # Рука вправе довести работу прямо в полученном документе, не создавая
        # answer.md заново (так ведёт себя theseus: правит v1.1 на месте).
        # Требовать именно answer.md — значит измерять не работу преемника, а
        # его готовность выполнить переименование; это тот же класс дефекта,
        # что D10 (raw-llm «отчитался» о файле, которого не мог создать).
        # Забираем v1.1 и отдельно фиксируем, был ли он вообще изменён:
        # у преемника, не тронувшего документ, все внесённые тезисы останутся
        # на месте и детектор покажет утечку 6/6 — то есть поблажки нет.
        answer = wip.read_text(encoding="utf-8", errors="replace")
        src = "wip"
        inj = cell / "injection.json"
        wip_modified = (inj.is_file()
                        and wip.stat().st_mtime > inj.stat().st_mtime)
        # Ошибку обнуляем ТОЛЬКО если преемник действительно правил документ.
        # Иначе поломка среды (например, прокси отверг запрос — theseus × glm,
        # D20) записывалась бы как сдача: сбор из документа возвращал бы
        # нетронутый v1.1, а поле `error` было бы пустым, и ячейка выглядела
        # бы как «рука ничего не сделала» вместо «рука не смогла начать».
        if wip_modified:
            err = None
    # Тот же структурный запрет, что и на стадии 1: текст, совпавший с входным
    # файлом бенчмарка, работой руки не считается (D21). На стадии 2 это
    # страхует случай, когда преемник просто скопировал планку приёмки в
    # финал, — тогда честнее сбой, чем «документ».
    if answer is not None and rm.is_seeded_text(work, answer):
        meta_err = "финал совпал с входным файлом бенчмарка (D21)"
        answer = None
    else:
        meta_err = err
    if answer is not None:
        (cell / "answer.stage2.md").write_text(answer, encoding="utf-8")
        # финал: answer.md на уровне ячейки = работа преемника
        # (версия стадии 1 уже сохранена как answer.v1.md)
        (cell / "answer.md").write_text(answer, encoding="utf-8")

    meta = {"cell": cell_name, "task": task, "arm": arm, "model": model,
            "stage": 2, "cmd": cmd_str, "secs": round(dt, 1), "exit_code": rc,
            "bytes": len(answer.encode("utf-8")) if answer else 0,
            "deliverable_source": src,
            "wip_modified": wip_modified,
            "resume_md_created": (resume.is_file()
                                  and resume.stat().st_mtime != before),
            "error": meta_err, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (cell / "meta2.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    with _log_lock:
        LOGS.mkdir(exist_ok=True)
        with (LOGS / "stage2.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    status = (f"ok {meta['bytes']}b {meta['secs']}s" if answer
              else f"ERR {str(meta_err)[:90]}")
    resume_note = "RESUME.md:да" if meta["resume_md_created"] else "RESUME.md:НЕТ"
    src_note = "" if meta["deliverable_source"] == "file" \
        else f" [{meta['deliverable_source']}"
    if meta["deliverable_source"] == "wip":
        src_note += ", правлен" if meta["wip_modified"] else ", НЕ правлен"
    src_note += "]" if src_note else ""
    print(f"{cell_name}: {status}{src_note}  {resume_note}", flush=True)
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-glob", default="*")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=1,
                    help="сколько преемников гонять параллельно (по умолчанию 1)")
    # --cells: явный список ячеек (можно с шаблонами). Нужен перегону: он
    # идёт по конкретным уличенным ячейкам, а шаблон руки захватил бы и те
    # ячейки, что ещё идут в волне, — одну ячейку повели бы два процесса, и
    # замер был бы испорчен молча.
    # Форма ОДНА на все три инструмента (reset_cells, prepare_stage2_rerun,
    # stage2_resume): одна строка через пробел, а не список аргументов.
    # Разнобой здесь уже один раз обнулил перегон: `--cells` без кавычек
    # рассыпался в 13 аргументов, argparse их отверг, и все три шага прошли
    # вхолостую — молча, если бы я не читал лог.
    ap.add_argument("--cells", default=None,
                    help="ячейки через пробел (можно с glob-шаблонами). "
                         "Пустая строка — «ничего», а не «всё»")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать, кого выбрали, и выйти (агенты не запускаются)")
    a = ap.parse_args()
    # ПУСТОЙ СПИСОК — ЭТО «НИЧЕГО», а не «всё». Иначе вызов с пустым набором
    # (например, из добивки финиша, где никого не нашлось) молча падал на маску
    # `*` и брал в работу все незакрытые ячейки стенда. «Всё» задаётся
    # отсутствием флага, а не его пустым значением.
    if a.cells is None:
        pats = None                      # флага не было — работаем по --only-glob
    else:
        pats = a.cells.split()           # флаг был: пусто = ничего
        if not pats:
            print("--cells пуст: выбирать нечего (это НЕ «все ячейки»)")
            return 0
    names = []
    for p in sorted(CELLS.iterdir()):
        if not p.is_dir():
            continue
        hit = (any(fnmatch.fnmatch(p.name, x) for x in pats) if pats
               else fnmatch.fnmatch(p.name, a.only_glob))
        if not hit:
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
    if a.dry_run:
        for n in names:
            print(f"    {n}")
        print("это выборка; агенты не запускались")
        return 0

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
