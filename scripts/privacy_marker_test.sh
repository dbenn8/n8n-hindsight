#!/usr/bin/env bash
# privacy_marker_test.sh — proves (or disproves) the PRIVACY.md claim that the
# server keeps no prompt text. Sends one recall and one validate-workflow request
# that carry unique marker strings, then counts the marker in every server-side
# store. Prints counts only; never prints log content. Exit 0 only when every
# content store shows 0. The IP row is informational (IP logging is disclosed).
#
# Usage: scripts/privacy_marker_test.sh            (needs ssh access to the host)
set -u
HOST="${PRIV_HOST:-app@178.104.162.81}"
KEY="${PRIV_SSH_KEY:-$HOME/.ssh/id_appliku}"
BASE="${PRIV_BASE:-https://n8nhindsight.applikuapp.com}"
WAIT="${PRIV_WAIT:-20}"
R=$(openssl rand -hex 6)
HEAD="PRIVHEAD${R}"   # inside the first 50 chars of the query
TAIL="PRIVTAIL${R}"   # after char 50: found only by stores that keep the full text
WF="PRIVWF${R}"       # inside a workflow sent to the validator
ML="PRIVML${R}"       # multi-line prompt: line 2 looks like a log line ("ERROR: ...")
MP="PRIVMP${R}"       # multi-line prompt: line 2 is plain text
TG="PRIVTG${R}"       # query of a recall that carries tag filters (structured recall)
TV="PRIVTV${R}"       # a tag value of that recall
Q="${HEAD} privacy marker test, please ignore this request, filler filler filler ${TAIL}"
echo "marker run ${R} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
c1=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "$BASE/public/recall" -H 'Content-Type: application/json' -d "{\"query\":\"$Q\"}")
c2=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "$BASE/public/validate-workflow" -H 'Content-Type: application/json' -d "{\"workflow\":{\"name\":\"$WF\",\"nodes\":[],\"connections\":{}}}")
# c5/c6 = multi-line prompts (newline inside the first 50 chars); c7 = recall with tag filters,
# the shape of the plugin's structured recall ({query, tags, tags_match, budget, max_tokens}).
post_json(){ python3 -c "import json,sys; print(json.dumps(eval(sys.argv[1])))" "$1" | curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "$BASE/public/recall" -H 'Content-Type: application/json' --data-binary @-; }
c5=$(post_json "{'query': 'privacy test a\\nERROR: ${ML} line two\\nline three'}")
c6=$(post_json "{'query': 'privacy test b\\n${MP} line two\\nINFO: line three'}")
c7=$(post_json "{'query': '${TG} privacy marker test with tags', 'tags': ['${TV}', 'type:node-spec'], 'tags_match': 'any', 'budget': 'low', 'max_tokens': 1024}")
echo "extra recall cases: multi-line log-like HTTP $c5, multi-line plain HTTP $c6, with tags HTTP $c7"
# Validator error paths and a large body (same marker, so the counts below cover them):
# c3 = large workflow (about 200 kB) with an unknown node type; c4 = malformed JSON.
BIG=$(mktemp); python3 -c "
import json,sys
m=sys.argv[1]
print(json.dumps({'workflow':{'name':m,'nodes':[{'id':'1','name':m,'type':'n8n-nodes-base.'+m,'typeVersion':1,'position':[0,0],'parameters':{'note':(m+' ')*12000}}],'connections':{m:{'main':[[{'node':'missing','type':'main','index':0}]]}}}}))" "$WF" > "$BIG"
c3=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "$BASE/public/validate-workflow" -H 'Content-Type: application/json' --data-binary @"$BIG"); rm -f "$BIG"
c4=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -X POST "$BASE/public/validate-workflow" -H 'Content-Type: application/json' -d "{\"workflow\":{\"name\":\"$WF\",")
echo "validator extra cases: large+unknown-node HTTP $c3, malformed HTTP $c4"
echo "recall http=$c1  validate-workflow http=$c2"
perl -e "select(undef,undef,undef,$WAIT)"
ssh -o ConnectTimeout=20 -i "$KEY" "$HOST" HEAD="$HEAD" TAIL="$TAIL" WF="$WF" ML="$ML" MP="$MP" TG="$TG" TV="$TV" bash -s <<'REMOTE'
n(){ grep -c -- "$1" 2>/dev/null || true; }
MARKS="HEAD TAIL WF ML MP TG TV"
sum=0
# row <label> <variable prefix>: prints one count per marker and adds them to the total
row(){ printf '%-48s' "$1"; for m in $MARKS; do eval c=\${$2_$m:-0}; printf ' %s=%-3s' "$(echo $m | tr A-Z a-z)" "$c"; sum=$((sum + c)); done; printf '\n'; }
sql(){ docker exec 4398-db-1 sh -c "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -Atc \"$1\""; }
# Read the Postgres log FIRST: the SQL searches below contain the markers and
# can themselves be logged as slow statements.
for m in $MARKS; do eval v=\$$m
  eval "p_$m=\$(docker logs --since 10m 4398-db-1 2>&1 | n $v)"
done
for m in $MARKS; do eval v=\$$m
  eval "a_$m=\$(sql \"select count(*) from audit_log where started_at > now() - interval '10 minutes' and (request::text like '%$v%' or response::text like '%$v%' or metadata::text like '%$v%')\")"
  eval "l_$m=\$(sql \"select count(*) from llm_requests t where t::text like '%$v%'\")"
  eval "o_$m=\$(sql \"select count(*) from async_operations t where t::text like '%$v%'\")"
  # Hindsight 1.x prints waiting DB statements ("[DB_WAITS] ... app=psql ... query=").
  # The SQL searches of THIS test hold the markers, so those lines are not counted.
  eval "f_$m=\$(docker exec n8nhindsight_web sh -c \"cat /data/logs/*.log | grep -v 'DB_WAITS.* app=psql ' | grep -c -- $v\")"
  eval "d_$m=\$(docker logs --since 10m n8nhindsight_web 2>&1 | grep -v 'DB_WAITS.* app=psql ' | n $v)"
  eval "h_$m=\$(docker exec nginx_nginx sh -c \"cat /var/log/nginx/*.log | grep -c -- $v\")"
  eval "g_$m=\$(docker logs --since 10m nginx_nginx 2>&1 | n $v)"
  eval "t_$m=\$(docker exec nginx_nginx sh -c \"grep -rl -- $v /var/cache/nginx 2>/dev/null | wc -l\")"
  tot=0; for c in $(docker ps --format '{{.Names}}' | grep -i valid); do x=$(docker logs --since 10m $c 2>&1 | n $v); y=$(docker exec $c sh -c "grep -rc -- $v /data/logs 2>/dev/null | awk -F: '{s+=\$NF} END{print s+0}'" 2>/dev/null); tot=$((tot + ${x:-0} + ${y:-0})); done
  eval "v_$m=$tot"
done
row "audit_log table (n8nhindsight DB)"            a
row "llm_requests table"                           l
row "async_operations table"                       o
row "/data/logs/*.log (app, inner nginx, proxies)" f
row "docker log: n8nhindsight_web"                 d
row "docker log: postgres 4398-db-1"               p
row "host nginx log files"                         h
row "docker log: nginx_nginx"                      g
row "host nginx temp files"                        t
row "validator containers (docker log + /data/logs)" v
echo "validator containers: $(docker ps --format '{{.Names}}' | grep -i valid | tr '\n' ' ')"
echo "TOTAL marker hits in content stores: $sum"
[ "$sum" -eq 0 ] && echo "PASS: no prompt text kept" || echo "FAIL: prompt text kept"
[ "$sum" -eq 0 ]
REMOTE
