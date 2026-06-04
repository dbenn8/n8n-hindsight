#!/bin/bash
# logpipe.sh <logname> <max_mb> <backups> <drop_regex|""> <cmd> [args...]
# Runs <cmd> and pipes its combined output to logwriter.py, which echoes EVERY
# line to stdout (so supervisord/Appliku keep the full live view, incl. the
# heartbeat) and writes the non-<drop_regex> lines to a rotating file at
# /data/logs/<logname>.log. logwriter is the last pipeline stage, so its stdout
# IS this script's stdout — no `tee`/`/dev/stdout` (which, mid-pipeline, would
# loop back into logwriter instead of reaching supervisord). Pair with
# stopasgroup/killasgroup in supervisord.
set -o pipefail
NAME="$1"; MAXMB="$2"; BACKUPS="$3"; DROP="$4"; shift 4
mkdir -p /data/logs
DROP_ARGS=()
[ -n "$DROP" ] && DROP_ARGS=(--drop "$DROP")
"$@" 2>&1 \
  | python3 /opt/logwriter.py --out "/data/logs/${NAME}.log" \
      --max-mb "$MAXMB" --backups "$BACKUPS" "${DROP_ARGS[@]}"
