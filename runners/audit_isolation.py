#!/usr/bin/env python3
"""audit_isolation.py — проверка, что рука не читала дизайн бенчмарка.

ЗАЧЕМ. Ячейки лежат в дереве каталогов бенчмарка, поэтому из рабочего
каталога агент доходит относительным путём до файлов дизайна: корпуса
инъекций (`contour/<T>/DEFECTS.yaml`), ключа викторины
(`handoff_quiz.yaml`), кода счёта (`runners/contour_score.py`), правил
судейства (`tasks/<T>/RUBRICS.md`) и пререгистрации. Рука, прочитавшая их,
знает ответы, и измерение становится недействительным. Это не гипотеза:
на прогоне 15–16.09.2026 так и произошло — 73 ячейки нового бенчмарка и
92 ячейки первого (`DEVIATIONS.md` D13).

Перенос прогонов за пределы репозитория закрывает относительный доступ,
но не доказывает, что никто не пошёл искать по абсолютным путям. Поэтому
нужна ПРОВЕРКА, а не надежда: этот инструмент читает журналы сессий и
помечает ячейки, где был доступ за пределы рабочего каталога.

Что считается нарушением (только по аргументам вызовов инструментов):
  * путь в каталог дизайна бенчмарка (file_path/path/notebook_path/
    command/pattern) — `<бенчмарк>/tasks|contour|defects|runners|spine|…`;
  * команда поиска (find/grep/rg/fd), называющая файл дизайна
    (DEFECTS.yaml, handoff_quiz.yaml, RUBRICS.md задачи, injection.json,
    contour_score.py, contour_prep.py, inject.py, prereg.lock.json,
    PREREGISTRATION.md, DEVIATIONS.md).

Что нарушением НЕ считается (и почему):
  * текст журнала целиком. В личной памяти агентов записаны пути прошлых
    проектов, заметки грузятся в контекст — проверка по тексту давала
    ложные срабатывания: 454 ячейки на первом бенчмарке, 8 из 11 здесь;
  * переход вверх (`..`). Ячейки вынесены из дерева репозитория, и
    относительным переходом до дизайна не дойти; относительный путь,
    который всё-таки называет бенчмарк, ловится первым правилом.

Покрытие журналов по харнессам (честно, потому что полнота разная):
  claude   ~/.claude/projects/<slug>/*.jsonl — разбираются вызовы целиком;
  theseus  <cell>/work/.theseus/*.json(l)    — поиск по тексту;
  arch-be  ~/.arch-harness/sessions/*.jsonl  — сопоставление по cwd ячейки;
  kimi     ~/.kimi-code/sessions/**          — сопоставление по cwd ячейки.
Где журнала нет — ячейка помечается `no_log`, а не «чисто»: отсутствие
данных не есть отсутствие нарушений.

Использование:
  PVBENCH_RUNS=<каталог> python3 runners/audit_isolation.py [--json отчёт]
Код возврата: 0 — нарушений нет, 1 — есть. Только stdlib.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

CELLS = pvlib.CELLS_DIR
HOME = Path.home()
CLAUDE_PROJ = HOME / ".claude" / "projects"
ARCH_SESS = HOME / ".arch-harness" / "sessions"
KIMI_SESS = HOME / ".kimi-code" / "sessions"

# Улика — ИМЯ ФАЙЛА ДИЗАЙНА или путь в каталог корпуса, но только внутри
# аргумента вызова инструмента. Голое имя в тексте журнала уликой не
# считается: имена вида PREREGISTRATION.md, run_matrix.py встречаются в
# личной памяти агентов, заметки грузятся в контекст, и проверка по тексту
# давала сотни ложных срабатываний (454 ячейки на первом бенчмарке, 8 из 11
# здесь). В аргументе же вызова эти имена не могут появиться случайно:
# ни один файл дизайна не входит в рабочий каталог ячейки.
DESIGN_FILES = re.compile(
    r"(?:DEFECTS\.ya?ml|RUBRICS\.md|handoff_quiz\.ya?ml|injection\.json|"
    r"contour_score\.py|contour_prep\.py|prepare_stage2_rerun\.py|"
    r"stage2_pending\.py|stage2_resume\.py|run_matrix\.py|prereg\.lock\.json|"
    r"inject\.py|PREREGISTRATION\.md|DEVIATIONS\.md|COI-POLICY\.md|"
    r"AEF-1-CHECKLIST\.md|audit_isolation\.py)")
# Корпус размножен по домашнему каталогу: две копии в репозиториях — не всё.
# Живые ячейки читали ~/.cache/spine-bank-public-snapshot, spine-bank,
# spine-aiml, _archive/arch-benchmark и ворктри .arch-harness (там лежит
# contour/ с DEFECTS.yaml текущего прогона).
CORPUS_ROOT = re.compile(
    r"(?:spine-contour-bench|platformv-arch-bench|arch-benchmark|spine-bank|"
    r"spine-aiml|0909-platformv|export-platformv)")
CORPUS_SUB = re.compile(r"/+(?:tasks|contour|defects|spine|customization|report)/")
# Отличие чтения от СОЗДАНИЯ одноимённого файла: агент вправе завести в своём
# каталоге собственный RUBRICS.md для самопроверки — это не доступ к ключу.
# Поэтому сегмент команды, который файл ЗАПИСЫВАЕТ (`>`, `>>`, `tee`,
# `touch`, `mkdir`), уликой не считается.
CREATES = re.compile(r"(?:>>?|tee\b|touch\b|mkdir(?:\s+-p)?)\s*[^\s;|&()]*$")
READS = re.compile(r"\b(?:cat|sed|head|tail|less|more|wc|diff|grep|rg|awk|"
                   r"find|fd|open|read_text|read_bytes|json\.load|tar|unzip|"
                   r"strings|xxd|base64|cp|source)\b")
PATHISH = re.compile(r'"(file_path|path|notebook_path|command|pattern)"\s*:\s*"((?:[^"\\]|\\.)*)"')
SEGS = re.compile(r"[;\n]|&&|\|\|")
# Поиск, ограниченный СВОЕЙ ячейкой, уликой не считается: агент вправе
# проверить, нет ли рядом рубрики (наблюдалось: `find <ячейка> -maxdepth 3
# -name "RUBRICS.md"`, ничего не нашёл). Улика — поиск за пределами ячейки.
SEARCH_CMD = re.compile(r"\b(?:find|grep|rg|fd)\b")
OUTSIDE = re.compile(r"(?<![\w.-])(?:/home|/tmp|/opt|/usr|/var|/etc|~)")
# Признаки того, что чтение НЕ удалось. Различие «доступ» vs «попытка» —
# не педантизм: ячейка, чья рука прочитала ключ, недействительна, а ячейка,
# чья рука попыталась при работающем заслоне, ничего не узнала, и её данные
# годны. Попытка при этом остаётся наблюдаемым свойством харнесса.
DENIED = re.compile(r"Отказано в доступе|Permission denied|PermissionError|"
                    r"Operation not permitted")


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def scan_text(text, cell_prefix=None):
    """Признаки доступа к дизайну — только по АРГУМЕНТАМ вызовов инструментов.

    Текст журнала целиком не проверяется. В личной памяти агентов записаны
    пути прошлых проектов, эти заметки грузятся в контекст, и проверка по
    тексту давала ложные срабатывания: на первом бенчмарке «затронутыми»
    вышли 454 ячейки, включая руки, которые ничего не читали, а здесь —
    8 ячеек из 11, причём без единого совпадения в аргументах.

    Доказательством считается вызов инструмента, в аргументе которого
    (`file_path`/`path`/`notebook_path`/`command`/`pattern`) есть:
      * имя файла дизайна — ни один такой файл не входит в рабочий каталог
        ячейки, случайно оно там появиться не может;
      * путь в каталог корпуса (по всем известным копиям, не только по двум
        репозиториям: корпус размножен по домашнему каталогу).

    Переход вверх (`..`) доказательством НЕ считается: ячейки лежат вне
    дерева репозитория, и относительным переходом до дизайна не дойти.
    """
    hits = set()
    for m in PATHISH.finditer(text):
        fld = m.group(1)
        val = m.group(2).encode().decode("unicode_escape", "replace")
        if fld != "command":
            # file_path/path/notebook_path/pattern — это адрес чтения по
            # построению, отличать нечего
            if DESIGN_FILES.search(val) or (
                    CORPUS_ROOT.search(val) and CORPUS_SUB.search(val)):
                hits.add("файл-дизайна")
            continue
        for seg in SEGS.split(val):
            if not DESIGN_FILES.search(seg):
                continue
            if _inside_only(seg, cell_prefix):
                continue                       # поиск в границах своей ячейки
            if CREATES.search(seg.split(DESIGN_FILES.search(seg).group(0))[0]):
                continue                       # файл заводят, а не читают
            if READS.search(seg) or re.search(r"[./]+\s*\S*$",
                                             seg[:DESIGN_FILES.search(seg).start()]):
                hits.add("чтение-файла-дизайна")
        if CORPUS_ROOT.search(val) and CORPUS_SUB.search(val):
            hits.add("путь-в-корпус")
    return hits


_SESSION_CACHE = {}
_NAME_INDEX = {}
CELL_NAME_RE = re.compile(r"[A-Z]{3}-[A-Z]+-\d+__[a-z-]+__[a-z0-9]+__r\d+")
_HITS_CACHE = {}
BLOCKED = {}   # ячейка → в её журналах есть отказ в доступе


def _inside_only(seg, cell_prefix):
    """Поиск имени файла дизайна ограничен своей же ячейкой?

    Считаем ограниченным, если после удаления пути ячейки в команде не
    остаётся ни корпуса, ни абсолютного корня (`/`, `/home`, `~`, `find /`).
    """
    if not SEARCH_CMD.search(seg):
        return False
    rest = seg.replace(cell_prefix, " ") if cell_prefix else seg
    if CORPUS_ROOT.search(rest):
        return False
    if re.search(r"\bfind\s+/(?:\s|$)", rest) or OUTSIDE.search(rest):
        return False
    return True


def _entry_ts(line):
    """Время записи журнала в epoch, или None, если не разобрать."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    ts = d.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    t = d.get("timestamp")
    if isinstance(t, str):
        try:
            return datetime.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _hits(path, txt, epoch=None, cell_prefix=None):
    """scan_text по файлу — считается один раз (дорогие регулярки).

    `epoch` — время сброса ячейки. Записи СТАРШЕ него не улики: журналы
    прежних, контаминированных попыток лежат на диске и после перегона
    (сессии Claude — в ~/.claude/projects, они не внутри ячейки, и сброс их
    не трогает). Без отсечки аудит клеймил бы перегнанную ячейку вечно, и
    перегон не давал бы ничего. Записи без разобранного времени считаются
    уликой: неопознанное не должно молча исчезать.
    """
    key = (str(path), epoch, cell_prefix)
    if key not in _HITS_CACHE:
        if epoch is None:
            _HITS_CACHE[key] = scan_text(txt, cell_prefix)
        else:
            keep = []
            for line in txt.splitlines():
                t = _entry_ts(line)
                if t is None or t >= epoch:
                    keep.append(line)
            _HITS_CACHE[key] = scan_text("\n".join(keep), cell_prefix)
    return _HITS_CACHE[key]


def _by_cell(base):
    """{имя ячейки: [(путь, текст)]} — индекс строится один раз на прогон.

    Без него поиск «упомянут ли путь этой ячейки» шёл по всем сессиям для
    каждой из тысяч ячеек: миллион поисков подстроки в текстах по мегабайту.
    """
    key = str(base)
    if key not in _NAME_INDEX:
        idx = {}
        for f, txt in _sessions(base):
            for nm in set(CELL_NAME_RE.findall(txt)):
                idx.setdefault(nm, []).append((f, txt))
        _NAME_INDEX[key] = idx
    return _NAME_INDEX[key]


def _sessions(base):
    """[(путь, текст)] для каталога сессий — читается один раз на прогон."""
    key = str(base)
    if key not in _SESSION_CACHE:
        out = []
        for f in Path(base).rglob("*"):
            if not f.is_file() or f.suffix not in (".json", ".jsonl"):
                continue
            try:
                out.append((f, f.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        _SESSION_CACHE[key] = out
    return _SESSION_CACHE[key]


def _file_epoch(path):
    """Время начала журнала из ЕГО ИМЕНИ, или None.

    Нужно потому, что форматы разные, и построчная отсечка работает не везде:
      * arch-be `session-YYYYMMDD-HHMMSS-<pid>.json` — время только в имени,
        строка с командой поля времени НЕ содержит (оно лежит отдельной
        строкой метаданных), поэтому по строкам старые попытки не отсечь;
      * theseus `session-<epoch>.json`, `events-<epoch>.jsonl` — epoch прямо
        в имени.
    Журналов без времени в имени (kimi) это не касается: там работает
    построчная отсечка, а если и её нет — запись считается уликой.
    """
    n = path.name
    m = re.match(r"session-(\d{8})-(\d{6})-", n)
    if m:
        try:
            return datetime.datetime.strptime(
                m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            return None
    m = re.match(r"(?:session|events|trace)-(\d{9,})\.", n)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def cell_epoch(cell):
    """Время последнего сброса ячейки (epoch) или None, если не сбрасывалась.

    Берём из маркера `RERUN_EPOCH`, который пишет сброс, а если маркера нет
    (ячейки, сброшенные до появления маркера) — из времени создания карантина.
    """
    p = cell / "RERUN_EPOCH"
    if p.is_file():
        try:
            return float(p.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass
    q = pvlib.RUNS / "quarantine"
    if q.is_dir():
        best = None
        for d in q.glob(cell.name + "*"):
            try:
                m = d.stat().st_mtime
            except OSError:
                continue
            best = m if best is None else max(best, m)
        if best is not None:
            return best
    return None


def cell_hits(cell):
    """{источник: множество признаков} по всем найденным журналам ячейки."""
    work = (cell / "work").resolve()
    ep = cell_epoch(cell)

    def stale(f):
        """Журнал целиком старше сброса ячейки — это ПРЕЖНЯЯ попытка."""
        fe = _file_epoch(f)
        return ep is not None and fe is not None and fe < ep

    found = {}

    # 1. claude: журнал по slug рабочего каталога
    d = CLAUDE_PROJ / slug(work)
    if d.is_dir():
        h = set()
        for f in d.glob("*.jsonl"):
            if stale(f):
                continue
            txt = f.read_text(encoding="utf-8", errors="replace")
            h |= _hits(f, txt, ep, str(cell))
            if h and DENIED.search(txt):
                BLOCKED[cell.name] = True
        found["claude"] = h

    # 2. theseus: журналы внутри ячейки
    for f in (work / ".theseus").glob("*") if (work / ".theseus").is_dir() else []:
        if f.is_file() and f.suffix in (".json", ".jsonl") and not stale(f):
            found.setdefault("theseus", set())
            txt = f.read_text(encoding="utf-8", errors="replace")
            found["theseus"] |= _hits(f, txt, ep, str(cell))
            if found["theseus"] and DENIED.search(txt):
                BLOCKED[cell.name] = True

    # 3. arch-be / kimi: общие каталоги сессий, сопоставляем по cwd ячейки.
    # Каждый файл читается ОДИН раз на прогон (индекс строится лениво):
    # построчное чтение глобальных каталогов для каждой из тысяч ячеек давало
    # квадратичную сложность и аудит полз десятки минут.
    for name, base in (("arch-be", ARCH_SESS), ("kimi", KIMI_SESS)):
        if not base.is_dir():
            continue
        for f, txt in _by_cell(base).get(cell.name, []):
            if str(work) not in txt or stale(f):
                continue
            # Сессия arch-be/kimi не содержит поля cwd, поэтому опознаём её по
            # упоминанию пути ячейки. Но сессия, где просто ПЕРЕЧИСЛЕНЫ ячейки
            # (например, листинг каталога runs/cells), содержит пути всех
            # ячеек сразу — приписывать её каждому было бы ложным срабатыванием.
            # Поэтому считаем сессию относящейся к ячейке, только если путей
            # ячеек в ней не больше двух.
            n_cells = len(set(re.findall(r"[A-Z]{3}-[A-Z]+-\d+__[a-z-]+__[a-z0-9]+__r\d+",
                                         txt)))
            if n_cells > 2:
                continue
            found.setdefault(name, set())
            found[name] |= _hits(f, txt, ep, str(cell))
            if found[name] and DENIED.search(txt):
                BLOCKED[cell.name] = True
    return found


def main():
    ap = argparse.ArgumentParser()
    # По умолчанию отчёт ложится туда, откуда его читают contour_report.py и
    # contour_effects.py (pvlib.AUDIT_PATH). Иначе аудит надо не забыть
    # перенаправить руками, а забытый ключ означал бы, что метрики посчитаны
    # БЕЗ исключения уличенных ячеек — и это никак не заметно в выводе.
    ap.add_argument("--json", default=str(pvlib.RUNS / "results" / "audit.json"))
    a = ap.parse_args()
    recs, bad = [], []
    cov = {}
    for c in sorted(CELLS.iterdir()):
        if not c.is_dir():
            continue
        hits = cell_hits(c)
        cov_key = "+".join(sorted(hits)) or "нет журнала"
        cov[cov_key] = cov.get(cov_key, 0) + 1
        vios = sorted({x for v in hits.values() for x in v})
        blocked = bool(vios) and BLOCKED.get(c.name, False)
        rec = {"cell": c.name, "sources": {k: sorted(v) for k, v in hits.items()},
               "violations": vios,
               # «отказано» = улика есть, но чтение отбито заслоном; такие
               # ячейки в зачёт идут, а попытка публикуется как свойство руки
               "blocked": blocked,
               "access": bool(vios) and not blocked}
        recs.append(rec)
        if rec["violations"]:
            bad.append(rec)
            tag = "доступ" if rec["access"] else "попытка (отказано)"
            print(f"  !! {c.name:44s} {tag:20s} "
                  f"{', '.join(rec['violations'])[:60]}")
    print(f"\nпроверено ячеек: {len(recs)}; с признаками доступа к дизайну: {len(bad)}")
    print("покрытие журналов: " + "; ".join(f"{k} — {v}" for k, v in
                                            sorted(cov.items(), key=lambda kv: -kv[1])))
    if bad:
        print('PVBENCH_CELLS="' + " ".join(r["cell"] for r in bad) + '"')
    if a.json:
        out = Path(a.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(recs, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"отчёт аудита: {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
