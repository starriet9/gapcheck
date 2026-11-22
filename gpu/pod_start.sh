#!/bin/bash
# In-pod start script (gpu/session.py sends it in the pod request as START_B64).
# No secret of the user is in here or in the pod.
#
# Env from the pod request (gpu/session.py builds them from configs/run1.json):
#   LLAMA_API_KEY    throwaway token; llama-server reads it from the environment (--api-key)
#   MODEL_REPO, MODEL_REV, MODEL_QUANT   Hugging Face repository, revision, quantization folder
#   MODEL_FILES      one line per GGUF file: "<name> <bytes> <sha256> <parallel ranges>";
#                    the first line is the file with the metadata (and the chat template)
#   TEMPLATE_SHA256  sha256 of the chat template (metadata key tokenizer.chat_template)
#   NP, CTX_PER_SLOT, EXTRA_ARGS, ALIAS   llama-server slots, context per slot, extra flags, alias
#   SELF_DESTRUCT_AT epoch seconds; IDLE_SECONDS: self-terminate after this long without a
#                    task once the server is up (0 = off)
# Steps: download every file (ranged, parallel, retried) -> sha256 of each (one re-download
# of a mismatched file) -> chat template extracted from file 1 -> its sha256 -> llama-server.
# A failed check writes $L/FATAL and starts no server; the session reads that file through
# the exec API, collects the logs and terminates the pod.
# Host checks (a failure writes $L/FATAL with a reason that starts with "host:"; the waiter
# gpu/wait_launch.sh then tries a new pod): the GPU is busy before any use (utilization >= 90 %
# with < 1 GiB used in every one of GPU_CHECK_SAMPLES samples), or the download is too slow
# (after DL_CHECK_S seconds the projected download time exceeds DL_MAX_S, or DL_MAX_S passes).
# Everything this script prints is also written to $L/start.log (collected with the logs).
# Guards in the pod (the local watchdog is the first layer):
#   layer 2: self-terminate at SELF_DESTRUCT_AT (after the watchdog's pod deadline)
#   layer 3: self-terminate when no slot has started a task for IDLE_SECONDS (client gone)
# Both use the pod-scoped key RunPod injects (RUNPOD_API_KEY), never the user's key.
# POD_START_TEST=1: define the functions only and return (local tests).
set -u
log(){ echo "[gapcheck $(date -u +%H:%M:%S)] $*"; }
D=${POD_DIR:-/models}; L=$D/logs

self_terminate(){ # best effort, repeated
  local i c c2
  for i in 1 2 3 4 5 6; do
    c=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer ${RUNPOD_API_KEY:-none}" "https://api.runpod.io/v2/pods/${RUNPOD_POD_ID:-none}")
    log "self-terminate DELETE -> $c"
    c2=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -H "Authorization: Bearer ${RUNPOD_API_KEY:-none}" \
         --data "{\"query\":\"mutation { podTerminate(input: {podId: \\\"${RUNPOD_POD_ID:-none}\\\"}) }\"}" https://api.runpod.io/graphql)
    log "self-terminate graphql -> $c2"
    sleep "${SELF_TERMINATE_PAUSE:-30}"
  done
}

idle_guard(){ # counts from the first healthy /health; activity = newest server log modified
  local up=0 t_up=0 last now age f
  while true; do
    sleep "${IDLE_POLL:-60}"
    if [ $up = 0 ]; then
      if [ "$(curl -s -o /dev/null -w '%{http_code}' "${HEALTH_URL:-http://127.0.0.1:8080/health}")" = 200 ]; then
        up=1; t_up=$(date +%s); log "idle guard armed: limit ${IDLE_SECONDS}s without a task"
      fi
      continue
    fi
    f=$(ls -t $L/server_np*.log 2>/dev/null | head -1)
    last=0; [ -n "$f" ] && last=$(stat -c %Y "$f")
    [ "$last" -lt "$t_up" ] && last=$t_up
    now=$(date +%s); age=$(( now - last ))
    if [ $age -ge "$IDLE_SECONDS" ]; then
      log "idle guard: no task for ${age}s >= ${IDLE_SECONDS}s: self-terminate"
      self_terminate
      return 0
    fi
  done
}

fatal(){ # refuse to start the server; the session sees $L/FATAL and tears the pod down
  mkdir -p "$L"
  log "FATAL $*"
  echo "$*" >> "$L/FATAL"
  [ "${POD_START_TEST:-0}" = 1 ] && return 1
  sleep infinity
}

# The string value of the GGUF metadata key tokenizer.chat_template, written to <out>.
# GGUF stores a key as u64 length + bytes, then u32 value type (8 = string), then the string
# as u64 length + bytes, little-endian. Every place in the first 64 MiB where the key's
# bytes occur is tried; only one preceded by its own length (23) and followed by type 8 is
# taken. gpu/extract_template.py is the full header reader that gives the same bytes; the
# sha256 check after this decides in any case.
extract_template(){ # gguf out -> 0 found, 1 not found
  local f=$1 out=$2 key=tokenizer.chat_template off kl typ len start
  for off in $(head -c 67108864 "$f" | LC_ALL=C grep -obaF "$key" | cut -d: -f1); do
    [ "$off" -ge 8 ] || continue
    kl=$(od -An -t u8 -j $((off - 8)) -N 8 "$f" | tr -d ' \n')
    [ "$kl" = "${#key}" ] || continue
    typ=$(od -An -t u4 -j $((off + ${#key})) -N 4 "$f" | tr -d ' \n')
    [ "$typ" = 8 ] || continue
    len=$(od -An -t u8 -j $((off + ${#key} + 4)) -N 8 "$f" | tr -d ' \n')
    start=$((off + ${#key} + 12))
    tail -c +$((start + 1)) "$f" | head -c "$len" > "$out"
    [ "$(wc -c < "$out")" = "$len" ] && return 0
  done
  return 1
}

template_from_gguf(){ # gguf out sha -> 0 when the extracted template has the expected sha256
  local got
  extract_template "$1" "$2.part" || { rm -f "$2.part"; fatal "no tokenizer.chat_template in $(basename "$1")"; return 1; }
  got=$(sha256sum "$2.part" | cut -d' ' -f1)
  if [ "$got" != "$3" ]; then
    rm -f "$2.part"
    fatal "chat template sha256 $got != expected $3"
    return 1
  fi
  mv "$2.part" "$2"
  log "chat template from $(basename "$1"): sha256 $got, $(wc -c < "$2") bytes"
}

dl_range(){ # url file start end
  local url=$1 f=$2 s=$3 e=$4 a rc
  for a in 1 2 3 4 5 6 7 8; do
    curl -fsSL --connect-timeout 30 --speed-time 60 --speed-limit 500000 -r "$s-$e" "$url" \
      | dd of="$f" bs=4M seek="$s" oflag=seek_bytes conv=notrunc iflag=fullblock status=none
    rc=("${PIPESTATUS[@]}")
    [ "${rc[0]}" = 0 ] && [ "${rc[1]}" = 0 ] && return 0
    log "range $(basename $f) $s-$e attempt $a failed (curl=${rc[0]} dd=${rc[1]}), retry"; sleep $((a*5))
  done
  return 1
}
dl_file(){ # name size nparts
  local n=$1 size=$2 parts=$3 f=$D/$1 chunk i s e pids=()
  truncate -s "$size" "$f"
  chunk=$(( (size + parts - 1) / parts ))
  for ((i=0; i<parts; i++)); do
    s=$((i*chunk)); e=$(( s + chunk - 1 )); [ $e -ge $size ] && e=$((size-1))
    dl_range "$BASE/$n" "$f" $s $e & pids+=($!)
  done
  local ok=0; for p in "${pids[@]}"; do wait $p || ok=1; done; return $ok
}
verify(){ # name sha
  local got; got=$(sha256sum "$D/$1" | cut -d' ' -f1)
  if [ "$got" = "$2" ]; then log "SHA OK $1 $got"; return 0; else log "SHA MISMATCH $1 got=$got want=$2"; return 1; fi
}

gpu_busy(){ # -> 0 (and the reason on stdout) when every sample shows a busy GPU before use
  local i u m seen=""
  for ((i=1; i<=${GPU_CHECK_SAMPLES:-3}; i++)); do
    [ $i -gt 1 ] && sleep "${GPU_CHECK_PAUSE:-15}"
    read -r u m < <(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ',')
    seen="$seen ${u:-?}%/${m:-?}MiB"
    { [ -n "${u:-}" ] && [ -n "${m:-}" ] && [ "$u" -ge 90 ] && [ "$m" -lt 1024 ]; } || return 1
  done
  echo "GPU busy before use (utilization/memory used:$seen)"
}

dl_too_slow(){ # got total elapsed -> 0 (and the reason on stdout) when the download is too slow
  local got=$1 total=$2 el=$3 chk=${DL_CHECK_S:-600} max=${DL_MAX_S:-2400} proj
  [ "$got" -ge "$total" ] && return 1
  if [ "$el" -ge "$max" ]; then echo "download not finished after ${el}s (limit ${max}s): $got of $total bytes"; return 0; fi
  [ "$el" -lt "$chk" ] && return 1
  if [ "$got" -le 0 ]; then echo "download too slow: 0 bytes after ${el}s"; return 0; fi
  proj=$(( el * total / got ))
  if [ "$proj" -gt "$max" ]; then
    echo "download too slow: $(( got / el / 1000000 )) MB/s after ${el}s, projected ${proj}s > ${max}s"; return 0
  fi
  return 1
}

[ "${POD_START_TEST:-0}" = 1 ] && return 0

mkdir -p $D $L
exec > >(tee -a "$L/start.log") 2>&1
log "start pod=${RUNPOD_POD_ID:-?} dc=${RUNPOD_DC_ID:-?} host_cuda=${CUDA_VERSION:-?} nproc=$(nproc)"
for v in MODEL_REPO MODEL_REV MODEL_QUANT MODEL_FILES TEMPLATE_SHA256; do
  [ -n "${!v:-}" ] || fatal "environment variable $v is not set"
done
nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader 2>&1 | sed 's/^/[gapcheck] gpu: /'
free -g | sed 's/^/[gapcheck] mem: /'; df -h / | sed 's/^/[gapcheck] disk: /'
cat /sys/fs/cgroup/memory.max 2>/dev/null | sed 's/^/[gapcheck] cgroup memory.max: /'

if [ -n "${SELF_DESTRUCT_AT:-}" ]; then
  ( s=$(( SELF_DESTRUCT_AT - $(date +%s) )); [ $s -lt 0 ] && s=0
    log "self-destruct armed in ${s}s"; sleep $s; self_terminate ) &
fi
if [ "${IDLE_SECONDS:-0}" -gt 0 ]; then idle_guard & fi
c=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${RUNPOD_API_KEY:-none}" "https://api.runpod.io/v2/pods/${RUNPOD_POD_ID:-none}")
log "pod-scoped key: GET own pod -> HTTP $c (key present: $([ -n "${RUNPOD_API_KEY:-}" ] && echo yes || echo no))"

why=$(gpu_busy) && fatal "host: $why"
log "GPU idle before use"

# GPU monitor every 60 s into a file
( while true; do echo "$(date -u +%H:%M:%S) $(nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,power.draw --format=csv,noheader 2>&1)" >> $L/gpu.log; sleep 60; done ) &

# ---- download the pinned GGUF files, ranged and parallel
BASE="https://huggingface.co/$MODEL_REPO/resolve/$MODEL_REV/$MODEL_QUANT"
FILES=$MODEL_FILES
FIRST=$(printf '%s\n' "$FILES" | head -1 | cut -d' ' -f1)
log "model $MODEL_REPO@$MODEL_REV $MODEL_QUANT: $(printf '%s\n' "$FILES" | wc -l) files"
T0=$(date +%s)
if [ ! -f $D/.verified ]; then
  TOTAL=$(printf '%s\n' "$FILES" | awk '{s += $2} END {printf "%d", s}')
  ( while [ ! -f $D/.dl_done ]; do sleep "${DL_PROGRESS_S:-30}"; [ -f $D/.dl_done ] && break
      got=$(du -s -B1 $D | cut -f1); el=$(( $(date +%s) - T0 ))
      log "download progress $got of $TOTAL bytes on disk, ${el}s"
      why=$(dl_too_slow "$got" "$TOTAL" "$el") && fatal "host: $why"
    done ) &
  dpids=(); while read -r n size sha parts; do dl_file "$n" "$size" "$parts" & dpids+=($!); done <<< "$FILES"
  for p in "${dpids[@]}"; do wait $p || log "a download job failed (sha256 check decides)"; done
  touch $D/.dl_done
  log "download finished in $(( $(date +%s) - T0 ))s"
  bad=0
  vpids=(); while read -r n size sha parts; do verify "$n" "$sha" > $L/sha_$n.txt 2>&1 & vpids+=($!); done <<< "$FILES"
  for p in "${vpids[@]}"; do wait $p; done
  cat $L/sha_*.txt
  grep -q MISMATCH $L/sha_*.txt && bad=1
  if [ $bad = 1 ]; then
    log "re-downloading mismatched files once"
    while read -r n size sha parts; do
      grep -q "MISMATCH $n" $L/sha_$n.txt && { dl_file "$n" "$size" "$parts" && verify "$n" "$sha" > $L/sha_$n.txt; cat $L/sha_$n.txt; }
    done <<< "$FILES"
    grep -q MISMATCH $L/sha_*.txt && fatal "GGUF sha256 mismatch after one re-download; not starting the server"
  fi
  touch $D/.verified
  log "GGUF verified in $(( $(date +%s) - T0 ))s total"
fi

# a host check failed while the download ran (the session terminates the pod): no server
[ -s "$L/FATAL" ] && { log "FATAL present: not starting the server"; sleep infinity; }

# ---- chat template: extracted from the verified first file, never sent in the request
template_from_gguf "$D/$FIRST" "$D/chat-template.jinja" "$TEMPLATE_SHA256"
/app/llama-server --version 2>&1 | sed 's/^/[gapcheck] version: /'

# ---- llama-server with the arguments from the pod request; restarted with the same
# arguments if it exits
np=${NP:-8}; cps=${CTX_PER_SLOT:-49152}; extra=${EXTRA_ARGS:-}
while true; do
  log "SERVER START np=$np ctx_per_slot=$cps total_ctx=$((np*cps)) extra='$extra'"
  # shellcheck disable=SC2086
  /app/llama-server -m $D/$FIRST \
    --alias "${ALIAS:-qwen3.8-flash-next-gpu}" --host 0.0.0.0 --port 8080 \
    -ngl 999 -fa on -np "$np" -c $((np*cps)) --no-kv-unified -b 2048 -ub 2048 \
    -ctk f16 -ctv f16 --fit off \
    --jinja --chat-template-file $D/chat-template.jinja --reasoning-format deepseek \
    --chat-template-kwargs '{"reasoning_effort": "medium"}' \
    --temp 1 --top-p 0.95 --top-k 20 --min-p 0 --repeat-penalty 1 --presence-penalty 0 \
    --predict 8192 --seed 1 --no-context-shift --cache-ram 8192 --metrics --no-webui $extra \
    > $L/server_np${np}_$(date -u +%H%M%S).log 2>&1
  log "SERVER EXIT code=$? (restarting in 3s with current args)"
  sleep 3
done
