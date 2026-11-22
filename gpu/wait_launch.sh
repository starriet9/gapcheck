#!/usr/bin/env bash
# Wait for the GPU, then start gpu/session.py once with a pod.
# From the environment: CASES (case set directory), RUN_DIR (session directory under
# data/runs/), CAP (USD for all pods of the run), EXTRA (more session.py arguments, e.g.
# "--protocol-tag <tag>", or "--cloud SECURE --max-hourly <USD>"). Defaults: one stock check
# every 5 minutes (POLL=300) for 24 hours (HOURS=24).
# Every check is logged to $RUN_DIR/wait.log (released with the run): UTC time, exit status,
# and the availability, count and price of the run's GPU on Community and on Secure Cloud,
# from the read-only gpu/gpu_check.py. No account amount is logged.
# The session is launched again (with --resume) when a launch ends without creating a pod (the
# stock went between the check and the create), and, up to HOST_RETRIES times (default 2), when
# it ends because of the pod's host: the pod refused to start the server with a "host:" reason
# (gpu/pod_start.sh: GPU busy before use, download too slow) or the server never became
# healthy. The next pod is a new one under the same cap. Stop waiting: touch $RUN_DIR.NO_LAUNCH
# Usage: CASES=data/cases/<set> RUN_DIR=data/runs/run1 CAP=45 EXTRA="--protocol-tag <tag>" \
#          setsid nohup gpu/wait_launch.sh > /dev/null 2>&1 < /dev/null &
set -u
cd "$(dirname "$0")/.." || exit 1
[ -f env.sh ] && source env.sh
: "${CASES:?set CASES=data/cases/<set>}" "${RUN_DIR:?set RUN_DIR=data/runs/<name>}" "${CAP:?set CAP=<USD>}"
EXTRA=${EXTRA:-}; POLL=${POLL:-300}; HOURS=${HOURS:-24}; HOST_RETRIES=${HOST_RETRIES:-2}; host_tries=0
mkdir -p "$RUN_DIR"
LOG=$RUN_DIR/wait.log
OUT=$RUN_DIR.out
deadline=$(( $(date +%s) + HOURS * 3600 ))
say() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
pods() { python3 -I -c "import json,sys; print(len(json.load(open(sys.argv[1]))['pods']))" "$RUN_DIR/ledger.json" 2>/dev/null || echo 0; }
status() { python3 -I -c "import json,sys; print(json.load(open(sys.argv[1])).get('status') or '')" "$RUN_DIR/session.json" 2>/dev/null; }
host_problem() { case "$1" in "stopped: pod refused to start the server: host:"*|"stopped: server not healthy after"*) return 0;; esac; return 1; }
CLOUD=COMMUNITY; printf '%s' "$EXTRA" | grep -q -- '--cloud SECURE' && CLOUD=SECURE
say "wait start pid=$$ poll=${POLL}s hours=$HOURS cloud=$CLOUD extra='$EXTRA'"
while :; do
  [ -e "$RUN_DIR.NO_LAUNCH" ] && { say "NO_LAUNCH found: exit"; exit 0; }
  out=$(uv run --locked python -I gpu/gpu_check.py --summary --cloud "$CLOUD" 2>&1); rc=$?
  info=$(printf '%s\n' "$out" | tail -1)
  say "check cloud=$CLOUD rc=$rc $info"
  if [ "$rc" -eq 0 ]; then
    extra=""; [ -e "$RUN_DIR/session.json" ] && extra="--resume"
    p0=$(pods)
    say "LAUNCH session.py --cases $CASES --run-dir $RUN_DIR --cap $CAP --go $extra $EXTRA"
    # shellcheck disable=SC2086
    uv run --locked python gpu/session.py --cases "$CASES" --run-dir "$RUN_DIR" --cap "$CAP" --go $extra $EXTRA \
      >> "$OUT" 2>&1 < /dev/null
    src=$?
    st=$(status)
    say "session exit $src, pods in ledger $(pods), status: $st"
    if [ "$(pods)" -gt "$p0" ]; then
      if host_problem "$st" && [ "$host_tries" -lt "$HOST_RETRIES" ]; then
        host_tries=$((host_tries + 1)); say "host problem: a new pod, retry $host_tries of $HOST_RETRIES"
      else
        say "a pod was created: waiter done"; exit 0
      fi
    fi
  fi
  [ "$(date +%s)" -ge "$deadline" ] && { say "deadline: exit"; exit 0; }
  sleep "$POLL"
done
