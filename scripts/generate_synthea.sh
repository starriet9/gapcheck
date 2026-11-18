#!/usr/bin/env bash
# Generate one Synthea v4.0.0 population with the settings of SPEC sec. 4.
#
# Usage: scripts/generate_synthea.sh <seed> <n_alive> <out_dir>
#   <out_dir> is relative to the repo root (for example data/dev/s20261120_p2500).
#
# Environment (optional):
#   THREADS   generator thread pool size (default 1; more threads change row
#             order and some aggregate files, so output is not byte-identical)
#   JAVA_XMX  JVM heap (default 32g)
#
# Writes <out_dir>/output/csv/*.csv, <out_dir>/synthea.log and
# <out_dir>/run_manifest.json (jar sha256, java version, argv, timestamps,
# sha256 of every CSV).
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <seed> <n_alive> <out_dir>" >&2
  exit 2
fi
SEED="$1"; N="$2"; OUT="$3"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

JAR="tools/synthea/v4.0.0/synthea-with-dependencies.jar"
JAR_SHA256="ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1"
JAVA="tools/jdk/bin/java"
# -r alone leaves the simulation end at the wall clock (open encounters get
# that time as STOP), so -e is pinned to the same date.
REFERENCE_DATE="20261118"
THREADS="${THREADS:-1}"
JAVA_XMX="${JAVA_XMX:-32g}"

case "$OUT" in
  /*) echo "out_dir must be relative to the repo root" >&2; exit 2 ;;
esac
if [ -e "$OUT/output/csv" ]; then
  echo "refusing to overwrite existing $OUT/output/csv" >&2
  exit 2
fi

echo "$JAR_SHA256  $JAR" | sha256sum -c --quiet -
mkdir -p "$OUT"

ARGS=(
  -Xmx"$JAVA_XMX" -jar "$JAR"
  -s "$SEED" -cs "$SEED" -r "$REFERENCE_DATE" -e "$REFERENCE_DATE" -p "$N" -a 30-79
  --exporter.baseDirectory="$OUT/output/"
  --exporter.years_of_history=0
  --exporter.csv.export=true
  --exporter.csv.excluded_files=claims_transactions.csv,claims.csv,patient_expenses.csv
  --exporter.fhir.export=false
  --exporter.hospital.fhir.export=false
  --exporter.practitioner.fhir.export=false
  --exporter.clinical_note.export=false
  --exporter.text.export=false
  --generate.thread_pool_size="$THREADS"
  --generate.log_patients.detail=none
  Massachusetts
)

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
"$JAVA" "${ARGS[@]}" > "$OUT/synthea.log" 2>&1
FINISHED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
tail -3 "$OUT/synthea.log"

JAVA_VERSION="$("$JAVA" -version 2>&1 | head -1)"
{
  printf '{\n'
  printf '  "generator": "synthea",\n'
  printf '  "jar": "%s",\n' "$JAR"
  printf '  "jar_sha256": "%s",\n' "$JAR_SHA256"
  printf '  "java": "%s",\n' "$JAVA"
  printf '  "java_version": %s,\n' "$(jq -Rn --arg v "$JAVA_VERSION" '$v')"
  printf '  "argv": %s,\n' "$(printf '%s\n' "$JAVA" "${ARGS[@]}" | jq -R . | jq -s -c .)"
  printf '  "seed": %s,\n  "n_alive": %s,\n  "reference_date": "%s",\n' "$SEED" "$N" "$REFERENCE_DATE"
  printf '  "started_utc": "%s",\n  "finished_utc": "%s",\n' "$STARTED" "$FINISHED"
  printf '  "csv_sha256": %s\n' "$(cd "$OUT/output/csv" && sha256sum -- *.csv \
      | jq -R 'split("  ") | {(.[1]): .[0]}' | jq -s -S 'add')"
  printf '}\n'
} > "$OUT/run_manifest.json"
echo "wrote $OUT/run_manifest.json"
