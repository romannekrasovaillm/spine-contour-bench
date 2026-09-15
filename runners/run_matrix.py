#!/usr/bin/env python3
"""run_matrix.py — прогон ячеек cells/ (основной массив + расширенный свип).

Параллелизм: ThreadPoolExecutor(6); семафоры: glm-канал (llm-proxy) ≤4,
deepseek-канал ≤5, claude-прокси ≤2, расширенный свип ≤2.
Таймаут ячейки 1200с (raw-llm — 600с), 2 ретрая (всего 3 попытки).
Пропуск ячейки, если answer.md уже есть и ≥ 500 байт (идемпотентность).
Ответ < 500 байт или rc != 0 считается сбоем.
meta.json: {task, condition, model, rep, cmd, secs, exit_code, bytes, ts}.
Лог: logs/generations.jsonl (append, под блокировкой).
Ключи не печатаются. Только stdlib.
"""
import fnmatch
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

BASE = pvlib.BASE
CELLS = pvlib.CELLS_DIR
LOGS = pvlib.LOGS_DIR
MIN_ANSWER_BYTES = 500
TIMEOUT_AGENTIC = int(os.environ.get("PVBENCH_TIMEOUT_AGENTIC", "3000"))
TIMEOUT_RAW = 600
RETRIES = int(os.environ.get("PVBENCH_RETRIES", "2"))
# Пауза между попытками, с (см. gen_cell): PVBENCH_RETRY_SLEEP секунд,
# линейно растущая с номером попытки.
RETRY_SLEEP = int(os.environ.get("PVBENCH_RETRY_SLEEP", "20"))

MODEL_IDS = {"dsf": "deepseek-flash", "glm": "glm-5.3-flash",
             "glm53": "glm-5.3", "dsp": "deepseek-v4-pro"}
ARCH_MODELS = {"dsf": "deepseek-bench", "glm": "glm-flash-bench",
               "glm53": "glm-5.3", "dsp": "deepseek-pro-bench"}
CLAUDE_MODELS = {"dsf": "deepseek-flash", "dsp": "deepseek-v4-pro",
               "glm": "glm-5.3-flash", "glm53": "glm-5.3"}
RAW_CFG = {
    "dsf": {"url": "https://api.deepseek.com/v1/chat/completions",
            "model": "deepseek-chat", "key_env": "DEEPSEEK_API_KEY",
            "max_tokens": 16000},
    "glm": {"url": "http://127.0.0.1:8795/v1/chat/completions",
            "model": "glm-5.3-flash", "key_env": "ZHIPU_API_KEY",
            "max_tokens": 32768, "stream": True, "effort": "low"},
    # glm53/dsp — ризонящие: reasoning съедает лимит, нужен запас
    "glm53": {"url": "http://127.0.0.1:8795/v1/chat/completions",
              "model": "glm-5.3", "key_env": "ZHIPU_API_KEY",
              "max_tokens": 65536, "stream": True, "effort": "low"},
    "dsp": {"url": "https://api.deepseek.com/v1/chat/completions",
            "model": "deepseek-v4-pro", "key_env": "DEEPSEEK_API_KEY",
            "max_tokens": 32000},
}
KIMI = os.environ.get("KIMI_BIN", "kimi")
THESEUS_MAX_TURNS = os.environ.get("PVBENCH_THESEUS_MAX_TURNS", "12")

SEM = {
    "deepseek": threading.Semaphore(5),  # arch-be dsf + theseus dsf + raw dsf
    "glm": threading.Semaphore(int(os.environ.get("PVBENCH_GLM_PAR", "4"))),
    "claude": threading.Semaphore(int(os.environ.get("PVBENCH_CLAUDE_PAR", "2"))),
    "sweep": threading.Semaphore(2),
}

# Токены не должны попадать в cmd (meta.json / generations.jsonl): ключ
# ZHIPU_API_KEY подставлялся в argv для glm-руки claude и утекал в артефакты
# прогона. Маскируем всё, что похоже на ANTHROPIC_AUTH_TOKEN=<значение>.
_SECRET_RE = re.compile(r"(ANTHROPIC_AUTH_TOKEN=)\S+")


def redact(s):
    return _SECRET_RE.sub(r"\1***", s)
_log_lock = threading.Lock()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def channel(cond, model):
    if cond.startswith(("dsh", "codewhale", "hermes")):
        return "sweep"
    if cond.startswith("claude"):
        return "claude"
    if model in ("glm", "glm53"):
        return "glm"
    return "deepseek"


def build_cmd(task, cond, model, prompt, uniq=""):
    """argv или None для raw-llm. В meta пишем cmd с $(cat prompt.txt)."""
    if cond in ("spine-arch", "spine-min"):
        argv = ["arch-be", "run", "-q", "--model", ARCH_MODELS[model],
                "--timeout", "2200"]
        # D10: deepseek-модели бенча — с --think off, иначе reasoning съедает
        # max_tokens и финальный документ не генерируется; у glm thinking
        # не отключается (HTTP 1210), флаг не добавляем.
        if model in ("dsf", "dsp"):
            argv += ["--think", "off"]
        return argv + [prompt]
    if cond == "spine-arch-think":
        # D11: рука «spine + ризонинг» (dsf only): max_tokens 65536 —
        # reasoning + финальный документ; сравнивается со spine-arch (off)
        # и с theseus/openclaw (у которых ризонинг max по дефолту).
        return ["arch-be", "run", "-q", "--model", "deepseek-bench-think",
                "--think", "on", "--timeout", "3600", prompt]
    if cond.startswith("theseus"):
        return ["theseus", "-m", MODEL_IDS[model], "--yolo",
                "--max-turns", THESEUS_MAX_TURNS, "-p", prompt]
    if cond.startswith("claude"):
        argv = ["claude", "-p", prompt, "--model", CLAUDE_MODELS[model],
                "--dangerously-skip-permissions"]
        if os.environ.get("PVBENCH_CLAUDE_STREAM"):
            # D22: text-режим отдаёт только ПОСЛЕДНЕЕ сообщение хода, поэтому
            # при упоре в лимит вывода начало документа теряется безвозвратно
            # (перегон это воспроизводит). stream-json отдаёт все сообщения;
            # сборка полного текста — extract_claude_stream(). stream-json
            # требует --verbose (иначе CLI выходит с ошибкой).
            argv += ["--output-format", "stream-json", "--verbose"]
        else:
            argv += ["--output-format", "text"]
        # D22: гасим несущественные вызовы CLI. Без этого он пытается
        # сгенерировать заголовок сессии «быстрой» моделью (в env машины это
        # flash[1m]), которой нет на стороне Z.AI, и падает с
        # [claude-code:unrecognized_model] {"query_source":"generate_session_title"} —
        # так потерялась ячейка DR-ARCH-001×claude-arch×glm×r2.
        envp = ["env", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
                # В env машины «быстрая» модель — flash (ANTHROPIC_DEFAULT_HAIKU_
                # MODEL=flash, ANTHROPIC_SMALL_FAST_MODEL=flash), а CLI гоняет
                # через неё фоновые вызовы (генерация заголовка сессии). На
                # сторонних endpoint'ах (Z.AI, deepseek-прокси) модели flash нет
                # -> [claude-code:unrecognized_model] {"query_source":
                # "generate_session_title"} и выход с кодом 1: так за прогон
                # потерялось несколько ячеек glm-руки. Приравниваем «быструю»
                # модель к основной для этой руки.
                f"ANTHROPIC_DEFAULT_HAIKU_MODEL={CLAUDE_MODELS[model]}",
                f"ANTHROPIC_SMALL_FAST_MODEL={CLAUDE_MODELS[model]}",
                # CLI не знает сторонних моделей (glm-5.3-flash, deepseek-*):
                # без этого флага он пытается вывести контекстное окно сам и
                # падает с [claude-code:unrecognized_model] {"query_source":"sdk"}
                # (так терялась DR-ARCH-001×claude-arch×glm×r2 и 4 ячейки dsp).
                "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1"]
        if model in ("glm", "glm53"):
            # D13: glm для claude — через Anthropic-совместимый endpoint Z.AI
            # (штатный claude-прокси машины обслуживает только deepseek-*)
            envp += ["ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic",
                     f"ANTHROPIC_AUTH_TOKEN={os.environ.get('ZHIPU_API_KEY', '')}"]
        return envp + argv
    if cond == "dsh-plain":
        return ["dsh", "--profile", "headless", prompt]
    if cond == "codewhale-plain":
        return ["codewhale", "exec", prompt]
    if cond == "hermes-plain":
        return ["hermes", "-z", prompt]
    if cond == "openclaw-plain":
        # сессионный ключ уникален на ячейку — иначе повторы продолжают
        # чужую сессию; модель фиксируем, чтобы условие попадало в канал dsf
        return ["openclaw", "agent", "--local", "-m", prompt, "--json",
                "--model", f"deepseek/{MODEL_IDS[model]}",
                "--session-key", f"pvbench-{uniq or task}"]
    if cond == "kimi-plain":
        provider = {"dsf": "deepseek", "glm": "zai", "glm53": "zai",
                    "dsp": "deepseek"}.get(model, "deepseek")
        return [KIMI, "-p", prompt, "-m", f"{provider}/{MODEL_IDS[model]}"]
    if cond == "qwen-plain":
        # qwen-code игнорирует modelProviders при заданном OPENAI_BASE_URL;
        # в env машины OPENAI_BASE_URL содержит битый суффикс /v1alias (404) —
        # переопределяем явно. Имя модели — API-идентификатор deepseek.
        return ["env", "OPENAI_BASE_URL=https://api.deepseek.com/v1",
                "OPENAI_MODEL=deepseek-flash",
                "qwen", "-p", prompt, "--approval-mode", "yolo"]
    if cond == "omp-plain":
        # pi-coding-agent: провайдер deepseek настроен в ~/.omp/agent/models.yml
        return ["omp", "--print", "--model", f"deepseek/{MODEL_IDS[model]}",
                prompt]
    return None  # raw-llm


def raw_call(model, prompt, timeout):
    cfg = RAW_CFG[model]
    key = os.environ.get(cfg["key_env"])
    if not key:
        return "", None, None, f"нет env {cfg['key_env']}"
    payload = {"model": cfg["model"],
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.7,
               "max_tokens": cfg.get("max_tokens", 16000)}
    if cfg.get("effort"):
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = cfg["effort"]
    # glm-семейство ризонит: нестриминговый ответ собирается дольше таймаута
    # прокси (503 ровно на 480с) — для них идём SSE и склеиваем delta.content
    if cfg.get("stream"):
        payload["stream"] = True
        try:
            req = urllib.request.Request(
                cfg["url"], data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
            parts, finish, model_out = [], None, None
            with urllib.request.urlopen(req, timeout=timeout) as r:
                for raw_line in r:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue  # keep-alive/неполные SSE-кадры прокси
                    model_out = chunk.get("model") or model_out
                    for ch in chunk.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            parts.append(delta["content"])
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
            return "".join(parts), model_out, finish, None
        except Exception as e:
            return "", None, None, f"{type(e).__name__}: {str(e)[:160]}"
    try:
        req = urllib.request.Request(
            cfg["url"], data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        ch = body["choices"][0]
        return ch["message"].get("content") or "", body.get("model"), \
            ch.get("finish_reason"), None
    except Exception as e:
        return "", None, None, f"{type(e).__name__}: {str(e)[:160]}"


def extract_theseus(work, stdout):
    """Ответ: последнее assistant-сообщение новейшей сессии .theseus/;
    если оно пустое/короткое — черновик *.md, который агент писал в work/;
    последний fallback — эвристика по stdout."""
    sd = work / ".theseus"
    try:
        sessions = sorted(sd.glob("session-*.json"),
                          key=lambda p: p.stat().st_mtime)
        if sessions:
            d = json.loads(sessions[-1].read_text(encoding="utf-8"))
            msgs = [m for m in d.get("messages", [])
                    if m.get("role") == "assistant"]
            for m in reversed(msgs):
                if len((m.get("content") or "").encode()) >= MIN_ANSWER_BYTES:
                    return m["content"]
    except Exception:
        pass  # fallback — черновик в work/
    cands = [p for p in work.glob("*.md")
             if p.name not in ("TASK.md", "CONTEXT.md", "AGENTS.md")
             and p.stat().st_size >= 2000]
    if cands:
        best = max(cands, key=lambda p: p.stat().st_mtime)
        return best.read_text(encoding="utf-8")
    clean = ANSI.sub("", stdout)
    lines = clean.splitlines()
    try:
        end = next(i for i, l in enumerate(lines) if l.startswith("⚙ finish"))
    except StopIteration:
        end = len(lines)
    body = lines[1:end] if lines and lines[0].startswith("❯") else lines[:end]
    body = [re.sub(r"\(мышление: \d+ символов\)\s*$", "", l) for l in body]
    return "\n".join(body).strip()


def extract_openclaw(stdout):
    d = json.loads(stdout)
    return "\n".join(p.get("text") or "" for p in d.get("payloads", []))


# Пинг harness'а Claude Code при упоре в лимит вывода (путь
# max_output_tokens_recovery): CLI продолжает генерацию НОВЫМ сообщением, а
# text/json-режимы отдают только последнее. См. D22.
CLAUDE_LIMIT_PING = "Output token limit hit"


def extract_claude_stream(stdout):
    """Полный текст ответа из потока stream-json (--verbose).

    Возвращает склейку последнего сообщения хода с теми предыдущими, после
    которых harness вбросил пинг лимита вывода. Для обычной сессии (документ
    в одном сообщении) результат совпадает с text-режимом — режимы эквивалентны
    везде, где потери не было. None, если поток не распознан.
    """
    msgs = []          # [[msg_id, text, шло_ли_после_пинга_лимита]]
    pending_ping = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "assistant":
            if d.get("parent_tool_use_id"):
                continue            # текст субагента — не часть ответа
            m = d.get("message") or {}
            mid = m.get("id") or d.get("uuid") or ""
            txt = "".join(b.get("text", "") for b in (m.get("content") or [])
                          if isinstance(b, dict) and b.get("type") == "text")
            if not txt.strip():
                continue            # thinking/tool_use-блоки
            if msgs and mid and msgs[-1][0] == mid:
                msgs[-1][1] += txt  # одно сообщение приходит блоками
            else:
                msgs.append([mid, txt, pending_ping])
            pending_ping = False
        elif t == "user":
            c = (d.get("message") or {}).get("content")
            txt = c if isinstance(c, str) else "".join(
                b.get("text", "") for b in (c or [])
                if isinstance(b, dict) and b.get("type") == "text")
            if CLAUDE_LIMIT_PING in txt:
                pending_ping = True
    if not msgs:
        # Поток не распознан (не наш формат/обрыв вывода): берём поле result
        # последней result-записи — это ровно то, что вернул бы text-режим.
        fallback = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "result" and isinstance(d.get("result"), str):
                fallback = d["result"].strip() or fallback
        return fallback
    parts, i = [msgs[-1][1]], len(msgs) - 1
    while i > 0 and msgs[i][2]:     # сообщение шло после пинга — не самостоятельное
        parts.insert(0, msgs[i - 1][1])
        i -= 1
    return "\n\n".join(p for p in parts if p.strip()).strip() or None


def run_once(task, cond, model, cell, prompt_name="prompt.txt"):
    """Одна попытка. Возвращает (answer|None, cmd_str, exit_code, note, err).

    prompt_name: какой файл промпта подать («prompt2.txt» — стадия 2,
    преемник; см. runners/stage2_resume.py).
    """
    work = cell / "work"
    prompt = (cell / prompt_name).read_text(encoding="utf-8")
    if cond == "raw-llm":
        answer, echo, finish, err = raw_call(model, prompt, TIMEOUT_RAW)
        cmd_str = f"raw POST {RAW_CFG[model]['url']} model={RAW_CFG[model]['model']}"
        note = f"echo={echo} finish={finish}"
        if not err and finish == "length":
            err = "ответ обрезан (finish=length)"
            answer = None  # обрезанный ответ считаем сбоем, см. PREREGISTRATION
        return (answer or None), cmd_str, (0 if not err else 1), note, err
    argv = build_cmd(task, cond, model, prompt, uniq=cell.name)
    cmd_str = redact(" ".join(a if a != prompt else "$(cat prompt.txt)"
                              for a in argv))
    try:
        # start_new_session + killpg: иначе внуки (node-субпроцессы theseus/
        # claude) держат pipe после kill прямого ребёнка, и communicate()
        # висит бесконечно (инцидент 11.09 — дедлок воркеров на 7.5 ч).
        proc = subprocess.Popen(argv, cwd=work, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            out, errout = proc.communicate(timeout=TIMEOUT_AGENTIC)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            # D22: второй communicate() тоже надо ограничивать. Если внуки
            # (node-процессы CLI) пережили killpg и держат пайпы, он висит
            # бесконечно — так 14.09 повисли три PGL-сессии на 9 часов, а
            # воркеры раннера встали в futex_wait. После ожидания закрываем
            # пайпы и уходим, не дожидаясь потомков.
            try:
                out, errout = proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                for f in (proc.stdout, proc.stderr):
                    try:
                        f.close()
                    except Exception:
                        pass
                proc.kill()
                return None, cmd_str, 124, None, \
                    f"timeout {TIMEOUT_AGENTIC}s (потомки не отвечают)"
            return None, cmd_str, 124, None, f"timeout {TIMEOUT_AGENTIC}s"
    except Exception as e:
        return None, cmd_str, 1, None, f"{type(e).__name__}: {str(e)[:160]}"
    if proc.returncode != 0:
        err = (errout or out)[-300:]
        # theseus exit 3 = лимит ходов: финального ответа нет, но агент мог
        # успеть записать черновик/ответ файлом в work/ — извлекаем его
        # (неполный ответ судится как есть, DEVIATIONS D7) вместо сжигания
        # повторных попыток по ~2.5M токенов.
        if cond.startswith("theseus"):
            salvaged = extract_theseus(work, out or "")
            if salvaged and len(salvaged.encode("utf-8")) >= MIN_ANSWER_BYTES:
                return salvaged, cmd_str, proc.returncode, \
                    f"salvage при exit {proc.returncode} (лимит ходов)", None
        return None, cmd_str, proc.returncode, None, err
    out = out or ""
    if cond.startswith("theseus"):
        answer = extract_theseus(work, out)
    elif cond == "openclaw-plain":
        try:
            answer = extract_openclaw(out)
        except Exception as e:
            return None, cmd_str, 0, None, f"json-parse: {str(e)[:120]}"
    elif cond == "kimi-plain":
        answer = re.sub(r"^• ", "", ANSI.sub("", out).strip())
    elif cond.startswith("claude") and os.environ.get("PVBENCH_CLAUDE_STREAM"):
        answer = extract_claude_stream(out) or out.strip()
    else:
        answer = out.strip()
    if len(answer.encode("utf-8")) < MIN_ANSWER_BYTES:
        return None, cmd_str, 0, None, \
            f"ответ слишком короткий ({len(answer.encode())} байт)"
    return answer, cmd_str, 0, None, None


def gen_cell(cell_name):
    m = re.match(r"^(.+?)__(.+)__(dsf|glm|glm53|dsp|default)__r(\d+)$", cell_name)
    task, cond, model, rep = m.group(1), m.group(2), m.group(3), int(m.group(4))
    cell = CELLS / cell_name
    t0 = time.time()
    answer, cmd_str, rc, note, err = None, "", 1, None, None
    sem = SEM[channel(cond, model)]
    for attempt in range(RETRIES + 1):
        with sem:
            answer, cmd_str, rc, note, err = run_once(task, cond, model, cell)
        if answer is not None:
            break
        # Пауза между попытками. При 429 со стороны провайдера (D22) каждая
        # попытка — это новая сессия, и она снова попадает в перегруженное окно;
        # PVBENCH_RETRY_SLEEP увеличивают, чтобы дождаться свободного окна.
        time.sleep(RETRY_SLEEP * (attempt + 1))
    dt = time.time() - t0
    meta = {"cell": cell_name, "task": task, "condition": cond, "model": model,
            "rep": rep, "cmd": cmd_str, "secs": round(dt, 1), "exit_code": rc,
            "bytes": len(answer.encode("utf-8")) if answer else 0,
            "note": note, "error": err,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    # D1 (этот бенчмарк): артефакт — файл в рабочем каталоге, а не stdout.
    # Так его видит и гейт (`arch-be control check .`), и преемник, и пакет
    # передачи. stdout остаётся фолбэком: рука, которая документ файлом не
    # записала, не выбрасывается, но помечается — это наблюдаемое свойство
    # харнесса, а не повод для повтора.
    src = "file"
    if answer is not None:
        wf = cell / "work" / "answer.md"
        if wf.is_file() and wf.stat().st_size >= MIN_ANSWER_BYTES:
            answer = wf.read_text(encoding="utf-8", errors="replace")
        else:
            src = "stdout"
            wf.write_text(answer, encoding="utf-8")
        meta["deliverable_source"] = src
        # bytes пересчитываем по фактическому артефакту: до этой строки там
        # лежал размер stdout (короткого подтверждения), что вводило в
        # заблуждение при разборе прогона
        meta["bytes"] = len(answer.encode("utf-8"))
        meta["stdout_bytes"] = len((out or "").encode("utf-8"))
        # копия на уровне ячейки: её читают judge.py/mech_score.py и аудит,
        # устроенные так же, как в platformv-arch-bench
        (cell / "answer.md").write_text(answer, encoding="utf-8")
    (cell / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    with _log_lock:
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / "generations.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    print(f"{cell_name}: {'ok ' + str(meta['bytes']) + 'b ' + str(meta['secs']) + 's' if answer else 'ERR ' + str(err)[:100]}",
          flush=True)


def main():
    LOGS.mkdir(exist_ok=True)
    cells = sorted(p.name for p in CELLS.iterdir() if p.is_dir())
    def done(name):
        ap = CELLS / name / "answer.md"
        return ap.is_file() and ap.stat().st_size >= MIN_ANSWER_BYTES
    # PVBENCH_SKIP_MODELS="glm glm53" — пропустить ячейки этих моделей
    # (например, пока upstream Z.AI деградирован); догоняются перезапуском.
    skip_models = set(os.environ.get("PVBENCH_SKIP_MODELS", "").split())
    # PVBENCH_ONLY="qwen-plain openclaw-plain" — прогнать только эти условия
    # (точечный догон новых ячеек параллельно с основным прогоном).
    only_conds = set(os.environ.get("PVBENCH_ONLY", "").split())
    def skipped(name):
        m = re.match(r"^(.+?)__(.+)__(dsf|glm|glm53|dsp|default)__r(\d+)$", name)
        return bool(m) and m.group(3) in skip_models
    def not_only(name):
        m = re.match(r"^(.+?)__(.+)__(dsf|glm|glm53|dsp|default)__r(\d+)$", name)
        return bool(only_conds) and bool(m) and m.group(2) not in only_conds
    # PVBENCH_CELLS="<glob> [<glob>…]" — точечный перегон отдельных ячеек
    # (например, ячеек с обрывом извлечения, D22), не трогая остальные.
    only_cells = os.environ.get("PVBENCH_CELLS", "").split()
    # PVBENCH_EXCLUDE_CELLS — исключить ячейки из прогона (напр. те, что
    # перегоняются отдельным запуском в режиме stream-json, D22).
    excl_cells = os.environ.get("PVBENCH_EXCLUDE_CELLS", "").split()
    def excluded(name):
        return bool(excl_cells) and any(fnmatch.fnmatch(name, g)
                                       for g in excl_cells)
    def not_cell(name):
        return bool(only_cells) and not any(fnmatch.fnmatch(name, g)
                                            for g in only_cells)
    todo = [c for c in cells if not done(c) and not skipped(c)
            and not not_only(c) and not not_cell(c) and not excluded(c)]
    print(f"ячеек всего: {len(cells)}, к прогону: {len(todo)}"
          f" (пропущено по PVBENCH_SKIP_MODELS: "
          f"{sum(1 for c in cells if not done(c)) - len(todo)})", flush=True)
    if "--dry-run" in sys.argv:
        return
    with ThreadPoolExecutor(
            max_workers=int(os.environ.get("PVBENCH_WORKERS", "8"))) as ex:
        list(ex.map(gen_cell, todo))
    failed = [c for c in todo if not done(c)]
    print(f"сбоев: {len(failed)}", flush=True)
    for c in failed:
        print(f"  FAIL {c}")


if __name__ == "__main__":
    main()
