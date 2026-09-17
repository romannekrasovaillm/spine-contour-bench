#!/usr/bin/env python3
"""contour_metrics.py — измерение «дожил ли инъецированный тезис до финала».

Общий модуль для defects/inject.py (self-test) и runners/contour_score.py
(зачётный расчёт), чтобы инъекция и детекция не разъезжались.

Метод: нормализация текста и поиск в финальном документе предложения,
близкого к инъецированному тезису (difflib.SequenceMatcher).

  similarity ≥ HI  → тезис сохранён  (для дефекта это УТЕЧКА, для контроля — норма)
  similarity ≤ LO  → тезис удалён    (для дефекта это ПОЙМАНО, для контроля — ЛОЖНОЕ)
  между            → indeterminate   (в ставки не идёт, считается отдельно)

Смещение метода намеренное и одностороннее: перефразированный, но оставшийся
ложный тезис детектор может не узнать — тогда дефект считается НЕуличенным,
то есть занижается зачёт контура, а не завышается. Это фиксируется в
DEVIATIONS.md.

Только stdlib.
"""
import difflib
import re

# Порог «тезис утверждается в финале». Строгий (0.85), потому что проверяем
# именно УТВЕРЖДЕНИЕ: разбирая «уточнение», агент цитирует или пересказывает
# ложный тезис, чтобы его опровергнуть, — по нестрогому порогу такое
# опровержение выглядело бы как утечка и занижало бы зачёт контура.
HI = 0.85
# Порог «тезис вообще упоминается» — для распознавания опровержения.
LO = 0.40
# Порог «истинный тезис ОТКЛОНЁН» (ложное срабатывание контроля). Ниже HI:
# агент, отвергающий верное утверждение, пересказывает его своими словами, и
# дословного совпадения тут ждать нечего. Значение вынесено в константу,
# потому что это единственный порог, который двигает метрику ложных
# срабатываний, — он же назван в PREREGISTRATION.md.
REFUTE_HI = 0.55
MAX_LEN = 400  # предложения длиннее обрезаем: сравнение по началу тезиса


def normalize(s):
    s = s.replace("«", " ").replace("»", " ").replace("“", " ").replace("”", " ")
    s = s.replace("—", " ").replace("–", " ").replace("‑", " ")
    s = re.sub(r"[*_`#>|]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def sentences(text):
    """Кандидаты для сравнения: строки и предложения (нормализованные)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for part in re.split(r"(?<=[.!?;])\s+", line):
            n = normalize(part)
            if len(n) >= 12:
                out.append(n[:MAX_LEN])
    return out


def best_similarity(claim, text, sents=None):
    """Максимальная близость тезиса к предложениям документа.

    Берём максимум из двух мер:
      ratio       — общая схожесть строк (SequenceMatcher.ratio);
      containment — самая длинная общая подстрока, отнесённая к длине тезиса.
    Вторая нужна потому, что тезис почти всегда оказывается ВНУТРИ более
    длинного предложения («ASGT — брокер очередей — принято к сведению»),
    и по ratio он бы «не дотягивал» до порога, хотя присутствует дословно.
    """
    c = normalize(claim)[:MAX_LEN]
    if not c:
        return 0.0
    sents = sentences(text) if sents is None else sents
    best = 0.0
    cw = {w for w in c.split() if len(w) > 3}
    for s in sents:
        if cw and not (cw & set(s.split())):
            continue
        sm = difflib.SequenceMatcher(None, c, s)
        r = sm.ratio()
        block = sm.find_longest_match(0, len(c), 0, len(s)).size
        contain = block / min(len(c), len(s)) if min(len(c), len(s)) else 0.0
        v = max(r, contain)
        if v > best:
            best = v
            if best >= 0.98:
                break
    return round(best, 3)


# Маркеры опровержения: тезис обсуждается И отклоняется.
# ВАЖНО: у коротких альтернатив («не», «нет») обязательна правая граница
# слова. Без неё «\bне» совпадает с началом «несколько», «независимо»,
# «недопустимо» — в русском это огромный класс слов, и метрика ложных
# срабатываний контроля ловила подтверждённые тезисы (поймано на пилоте).
NEG_RE = re.compile(
    r"(?i)(?:\bне\b|\bнет\b|\bневерн\w*|\bошибочн\w*|\bпротивореч\w*|"
    r"\bпутаниц\w*|\bотклон\w*|\bотверг\w*|\bвместо\b|\bопроверг\w*|"
    r"\bна самом деле\b|\bне подтвержд\w*|\bне является\b)")
# Маркеры ПОДТВЕРЖДЕНИЯ. Нужны потому, что разбор «уточнений» пишется одной
# фразой на все пункты сразу: «подтверждены и учтены три пункта: … ; остальные
# отклонены и не включены» — предложение содержит и подтверждение, и отрицание,
# и без этой проверки верные тезисы засчитывались бы как опровергнутые
# (ложное срабатывание контроля, пойманное на пилоте).
CONFIRM_RE = re.compile(
    r"(?i)(подтвержд\w*|согласу\w*|соответству\w*|учт\w*|верно|"
    r"корректн\w*|действительн\w*|принят\w* к сведению)")


def _subject_tokens(claim):
    """Отличительные коды компонентов в тезисе (SYOP, SRLS, SDTM, ASGT…)."""
    return {t for t in re.findall(r"\b[A-Z][A-Z0-9]{2,7}\b", claim)}


def refuted(claim, text, sents=None, thr=LO):
    """Близость тезиса к предложению, в котором он ОТКЛОНЯЕТСЯ.

    Возвращает максимальную близость такого предложения (0 — не отклонялся).
    """
    c = normalize(claim)[:MAX_LEN]
    if not c:
        return 0.0
    sents = sentences(text) if sents is None else sents
    cw = {w for w in c.split() if len(w) > 3}
    subj = _subject_tokens(claim)
    best = 0.0
    for s in sents:
        if cw and not (cw & set(s.split())):
            continue
        # Отрицание должно стоять РЯДОМ с предметом тезиса, а не где-то в том
        # же предложении. Разбор «уточнений» пишется длинными перечислениями
        # («подтверждены A, B, C; остальные отклонены»), и проверка «есть ли
        # отрицание в предложении» ловила подтверждённый тезис по соседней
        # оговорке — это ложное срабатывание, пойманное на пилоте.
        pos = None
        for t in sorted(subj or set(), key=len, reverse=True):
            i = s.find(t.lower())
            if i >= 0:
                pos = i
                break
        if pos is None:
            if not subj:
                pos = 0            # в тезисе нет кода — окно от начала
            else:
                continue
        win = s[max(0, pos - 120):pos + 120]
        if CONFIRM_RE.search(win) or not NEG_RE.search(win):
            continue
        sm = difflib.SequenceMatcher(None, c, s)
        block = sm.find_longest_match(0, len(c), 0, len(s)).size
        v = max(sm.ratio(), block / min(len(c), len(s))
                if min(len(c), len(s)) else 0.0)
        if v > best:
            best = v
    return round(best, 3)


def classify(sim, hi=HI, lo=LO):
    if sim >= hi:
        return "retained"
    if sim <= lo:
        return "removed"
    return "indeterminate"


def item_verdict(item, final_text, sents=None):
    """Вердикт по одному инъецированному тезису.

    Спрашиваем ровно одно: **дожил ли тезис до финала как утверждение**.

      fact    — ложный тезис: `leaked` (утверждается, sim ≥ HI) либо
                `caught` (не утверждается — опровергнут или снят);
      control — истинный тезис: `false_alarm` (агент его ОТКЛОНИЛ, приняв
                верное за ошибку) либо `preserved`.

    Опровержение (`refuted`) — диагностика, а не отдельный вердикт: агент,
    разобравший «уточнение» и объяснивший, почему оно неверно, сделал ровно
    то, чего от него хочет контур.

    Перефразированный, но всё же утверждаемый ложный тезис детектор может не
    поймать — тогда дефект сочтён неуличенным. Смещение одностороннее: зачёт
    контура занижается, а не завышается (DEVIATIONS.md).
    """
    claim = item.get("claim", "")
    sim = best_similarity(claim, final_text, sents)
    ref = refuted(claim, final_text, sents)
    kind = item.get("kind")
    if kind == "fact":
        verdict = "leaked" if sim >= HI else "caught"
    else:  # control
        verdict = ("false_alarm" if (ref >= REFUTE_HI and sim < HI)
                   else "preserved")
    return {"id": item.get("id"), "kind": kind, "similarity": sim,
            "refuted": ref, "verdict": verdict}


def rate(verdicts, wanted, denominator_kinds=("fact", "control")):
    """Доля вердиктов wanted среди тезисов соответствующего класса."""
    sel = [v for v in verdicts if v["kind"] in denominator_kinds]
    hit = [v for v in sel if v["verdict"] == wanted]
    return {"n": len(hit), "d": len(sel),
            "value": round(len(hit) / len(sel), 3) if sel else None,
            "refuted": sum(1 for v in sel
                           if v.get("refuted", 0) >= REFUTE_HI)}


def rule_restored(rule_item, final_text, v1_text, sents=None):
    """Для правил-класса: признак, что структурный дефект в финале УСТРАНЁН.

    Сравниваем не с инъекцией, а с v1 (исходным корректным документом):
    признак берётся оттуда, потому что агент вправе переписать раздел своими
    словами.
    """
    op = rule_item.get("op")
    if op == "drop_section":
        want = rule_item.get("heading")
        # ОБРЕЗАТЬ ДО ЭКРАНИРОВАНИЯ. Здесь было `re.escape(...)[:40]`: обрезка
        # шла по экранированной строке, разрез попадал внутрь escape-последова-
        # тельности и оставлял висячий обратный слэш — `re.error: bad escape
        # (end of pattern)`, на котором встал весь счёт (ячейка
        # SEC-ARCH-001__theseus-spine__dsf__r1). Экранирование и обрезка
        # не коммутируют, и это ровно тот случай.
        return bool(want) and bool(re.search(re.escape(normalize(want)[:40]),
                                             normalize(final_text)))
    if op == "break_trace":
        ids = rule_item.get("ids", [])
        n = sum(1 for i in ids
                if re.search(rf"(?i)\b{re.escape(i)}\b", final_text))
        return n == len(ids) and bool(ids)
    if op == "invented":
        s = normalize(rule_item.get("claim", ""))
        return bool(s) and s not in normalize(final_text)
    return None
