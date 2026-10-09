#!/bin/sh
# Container entrypoint: decode config values that Appliku cannot carry verbatim, then exec supervisord.
#
# Appliku escapes double quotes inside config-var values (a value of {"mode": "failover"} reaches the
# container as {\"mode\": \"failover\"}), so a JSON-valued Hindsight setting such as
# HINDSIGHT_API_CONSOLIDATION_LLM_STRATEGY cannot be set directly: the API fails fast at startup on
# invalid JSON (2026-10-09). Any *_B64 variable listed below is base64-decoded into the real name here.
set -e
for name in HINDSIGHT_API_CONSOLIDATION_LLM_STRATEGY HINDSIGHT_API_RETAIN_LLM_STRATEGY HINDSIGHT_API_LLM_STRATEGY; do
  b64="$(printenv "${name}_B64" 2>/dev/null || true)"
  if [ -n "$b64" ]; then
    export "$name"="$(printf '%s' "$b64" | base64 -d)"
    echo "[start] $name decoded from ${name}_B64"
  fi
done
exec supervisord -c /etc/supervisord.conf
