#!/usr/bin/env bash
# Стадия 2 ячейки CMP-ARCH-001 (spine-arch / dsf / r2) тем же кодовым харнессом
# и промптом, что в бенчмарке: arch-be run, модель deepseek (deepseek-flash),
# --think off; рабочий каталог — work/ ячейки; сессии стадии 1 нет.
set -u
CELL="/home/roman/experiments/spine-contour-bench/runs/cells/CMP-ARCH-001__spine-arch__dsf__r2"
WORK="$CELL/work"
cd "$WORK" || exit 3
echo "[run] cwd=$WORK ($(pwd -P))"
[ -f WORK-IN-PROGRESS.md ] || { echo "[run] НЕТ WORK-IN-PROGRESS.md"; exit 4; }
PROMPT="$(cat "$CELL/prompt2.txt")"
arch-be run -q --model deepseek --timeout 2200 --think off "$PROMPT" > "$CELL/answer.stage2.md" 2> "$CELL/.stage2.stderr"
rc=$?
echo "[run] arch-be run exit=$rc; stdout bytes=$(wc -c < "$CELL/answer.stage2.md")"
echo "--- stderr (tail)"; tail -6 "$CELL/.stage2.stderr"
echo "--- work/"; ls -la "$WORK"
exit $rc
