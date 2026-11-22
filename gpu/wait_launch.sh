#!/usr/bin/env bash
# Wait for the GPU, then start gpu/session.py once with a pod.
# From the environment: CASES (case set directory), RUN_DIR (session directory under
# data/runs/), CAP (USD for all pods of the run), EXTRA (more session.py arguments, e.g.
# "--protocol-tag <tag>", or "--cloud SECURE --max-hourly <USD>"). Defaults: one stock check
# every 5 minutes (POLL=300) for 24 hours (HOURS=24).
# Every check is logged to $RUN_DIR/wait.log (released with the run): UTC time, exit status,
# and the availability, count and price of the run's GPU on Community and on Secure Cloud,
# from the read-only gpu/gpu_check.py. No account amount is logged.
# The session is launched again (with --resume) only when a launch ends without creating a
# pod (the stock went between the check and the create). Stop waiting: touch $RUN_DIR.NO_LAUNCH
# Usage: CASES=data/cases/<set> RUN_DIR=data/runs/run1 CAP=45 EXTRA="--protocol-tag <tag>" \
#          setsid nohup gpu/wait_launch.sh > /dev/null 2>&1 < /dev/null &
set -u
cd "$(dirname "$0")/.." || exit 1
[ -f env.sh ] && source env.sh
: "${CASES:?set CASES=data/cases/<set>}" "${RUN_DIR:?set RUN_DIR=data/runs/<name>}" "${CAP:?set CAP=<USD>}"
EXTRA=${EXTRA:-}; POLL=${POLL:-300}; HOURS=${HOURS:-24}
mkdir -p "$RUN_DIR"
LOG=$RUN_DIR/wait.log
OUT=$RUN_DIR.out
deadline=$(( $(date +%s) + HOURS * 3600 ))
say() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
pods() { python3 -I -c "import json,sys; print(len(json.load(open(sys.argv[1]))['pods']))" "$RUN_DIR/ledger.json" 2>/dev/null || echo 0; }
CLOUD=COMMUNITY; printf '%s' "$EXTRA" | grep -q -- '--cloud SECURE' && CLOUD=SECURE
say "wait start pid=$$ poll=${POLL}s hours=$HOURS cloud=$CLOUD extra='$EXTRA'"
while :; do
  [ -e "$RUN_DIR.NO_LAUNCH" ] && { say "NO_LAUNCH found: exit"; exit 0; }
  out=$(uv run --locked python -I gpu/gpu_check.py --summary --cloud "$CLOUD" 2>&1); rc=$?
  info=$(printf '%s\n' "$out" | tail -1)
  say "check cloud=$CLOUD rc=$rc $info"
  if [ "$rc" -eq 0 ]; then
    extra=""; [ -e "$RUN_DIR/session.json" ] && extra="--resume"
    say "LAUNCH session.py --cases $CASES --run-dir $RUN_DIR --cap $CAP --go $extra $EXTRA"
    # shellcheck disable=SC2086
    uv run --locked python gpu/session.py --cases "$CASES" --run-dir "$RUN_DIR" --cap "$CAP" --go $extra $EXTRA \
      >> "$OUT" 2>&1 < /dev/null
    src=$?
    say "session exit $src, pods in ledger $(pods)"
    if [ "$(pods)" -gt 0 ]; then say "a pod was created: waiter done"; exit 0; fi
  fi
  [ "$(date +%s)" -ge "$deadline" ] && { say "deadline: exit"; exit 0; }
  sleep "$POLL"
done
