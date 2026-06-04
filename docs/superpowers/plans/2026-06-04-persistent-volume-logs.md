# Persistent Volume Logs (n8n-hindsight) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline). In Dan's environment **subagents cannot run Bash**, so all test/commit/deploy steps run in the main session. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replicate the durable-logging + hardened query endpoint already shipped on the personal instance (`../hindsight-deploy`) onto **n8n-hindsight** (Appliku app **4460**, domain **n8nhindsight.applikuapp.com**, branch **master**, auto-deploys on push).

**Architecture:** Each supervisord program's output is split — full stream to stdout (Appliku live view, heartbeat intact) and a filtered copy via `logpipe.sh → logwriter.py` to a size-rotated file under `/data/logs/`. A standalone, hardened `ops-proxy` FastAPI service (`:8001`, gated by `LOGS_ADMIN_KEY`) serves `GET /logs?service=&grep=&lines=`.

**Tech Stack:** Python stdlib (`RotatingFileHandler`), bash, supervisord, FastAPI + slowapi (ops-proxy), nginx (envsubst template), Appliku.

**Source of truth (verbatim copies):** `/Users/danielbennett/codeNew/hindsight-deploy` — the same artifacts, built + verified in production June 4 2026. This plan copies the deployment-agnostic pieces and writes only the n8n-hindsight-specific wiring.

---

## What's the SAME vs DIFFERENT from the personal instance

**Same (copy verbatim):** `logwriter/logwriter.py` (+ tests), `logpipe.sh`, `ops-proxy/` (app.py, requirements.txt, tests). The `WORKER_STATS` heartbeat drop (hindsight-api emits it here too, same image).

**Different (account for these):**
- n8n-hindsight has **only 3 web-container programs**: `nginx` (run via `/usr/local/bin/start-nginx.sh`), `hindsight-api`, `control-plane`. No Hermes/meridian/burrfect-proxy.
- `nginx.conf` is an **envsubst template** rendered by `start-nginx.sh` with a **restricted** var list (`'${HINDSIGHT_API_TENANT_API_KEY}'`), so nginx vars like `$host` are preserved — safe to add a `/logs` location.
- **No `burrfect-proxy`**, so ops-proxy is the only FastAPI service: its deps (fastapi/uvicorn/slowapi) must be pip-installed fresh (its `requirements.txt` covers all three). Run via system `python3.11` (same base image).
- **No local test venv** in this repo — reuse the sibling one: `../hindsight-deploy/burrfect-proxy/.venv-test/bin/python` (already has pytest+fastapi+slowapi+httpx+respx).
- **Scope boundary:** the 4 `cronjobs:` (github/docs/community/releases sync) run as **separate Appliku one-off containers**, NOT in the web container's supervisord — they are NOT covered by logpipe (and per a known Appliku caveat, one-off containers don't reliably share the web container's `/data` volume). Durable logs cover the **web-container services only**. Cron output stays in Appliku's per-cron logs. This is acceptable; do not try to wire the crons.

---

## File Structure

- Copy → `logwriter/logwriter.py`, `logwriter/tests/test_logwriter.py`
- Copy → `logpipe.sh`
- Copy → `ops-proxy/app.py`, `ops-proxy/requirements.txt`, `ops-proxy/tests/test_logs.py`
- Modify → `supervisord.conf` (wrap 3 programs via logpipe + add `[program:ops-proxy]`)
- Modify → `nginx.conf` (add `opsproxy` upstream + `location /logs`)
- Modify → `Dockerfile` (COPY artifacts, pip install ops-proxy reqs, mkdir `/data/logs`)
- Modify → `appliku.yml` (declare `LOGS_ADMIN_KEY`)

All paths below are relative to `/Users/danielbennett/codeNew/n8n-hindsight`.

---

### Task 1: Copy the deployment-agnostic artifacts + confirm their tests pass

- [ ] **Step 1: Copy verbatim from the sibling repo**

```bash
cd /Users/danielbennett/codeNew/n8n-hindsight
mkdir -p logwriter/tests ops-proxy/tests
cp ../hindsight-deploy/logwriter/logwriter.py            logwriter/logwriter.py
cp ../hindsight-deploy/logwriter/tests/test_logwriter.py logwriter/tests/test_logwriter.py
cp ../hindsight-deploy/logpipe.sh                        logpipe.sh
cp ../hindsight-deploy/ops-proxy/app.py                  ops-proxy/app.py
cp ../hindsight-deploy/ops-proxy/requirements.txt        ops-proxy/requirements.txt
cp ../hindsight-deploy/ops-proxy/tests/test_logs.py      ops-proxy/tests/test_logs.py
chmod +x logpipe.sh
```

- [ ] **Step 2: Run both test suites using the sibling venv (zero setup)**

```bash
cd /Users/danielbennett/codeNew/n8n-hindsight
../hindsight-deploy/burrfect-proxy/.venv-test/bin/python -m pytest logwriter/tests/test_logwriter.py -q
cd ops-proxy && ../../hindsight-deploy/burrfect-proxy/.venv-test/bin/python -m pytest tests/test_logs.py -q
```
Expected: `4 passed` then `9 passed`.

- [ ] **Step 3: Sanity-check the logpipe pipeline end-to-end**

```bash
cd /Users/danielbennett/codeNew/n8n-hindsight
rm -f /tmp/t.log
printf 'alpha\nWORKER_STATS hb\nbravo\n' | python3 logwriter/logwriter.py --out /tmp/t.log --max-mb 5 --backups 1 --drop WORKER_STATS
cat /tmp/t.log   # expect exactly: alpha / bravo  (no WORKER_STATS, no dupes)
```
Expected stdout shows all 3 lines; `/tmp/t.log` has only `alpha` and `bravo`.

- [ ] **Step 4: Commit**

```bash
git add logwriter logpipe.sh ops-proxy
git commit -m "Add durable logging artifacts (logwriter, logpipe, ops-proxy) — ported from hindsight-deploy"
```

---

### Task 2: Wire the 3 web programs through logpipe + add ops-proxy

**File:** `supervisord.conf`

- [ ] **Step 1: Replace each program's `command=` and add group-signal flags**

Edit the three existing blocks' `command=` lines and add `stopasgroup=true` + `killasgroup=true` to each (keep every other directive unchanged):

```ini
[program:nginx]
command=/usr/local/bin/logpipe.sh nginx 12 1 "" /usr/local/bin/start-nginx.sh
# (keep autostart/autorestart/logfile lines) + add:
stopasgroup=true
killasgroup=true

[program:hindsight-api]
command=/usr/local/bin/logpipe.sh hindsight-api 60 5 "WORKER_STATS" hindsight-api
stopasgroup=true
killasgroup=true

[program:control-plane]
command=/usr/local/bin/logpipe.sh control-plane 12 1 "" hindsight-control-plane
stopasgroup=true
killasgroup=true
# (keep the existing environment= line on control-plane unchanged)
```

- [ ] **Step 2: Append the ops-proxy program at the end of `supervisord.conf`**

```ini
[program:ops-proxy]
# Standalone admin service: GET /logs (durable-log retrieval). Admin-key gated
# (LOGS_ADMIN_KEY), rate-limited, read-only. Runs via system python3.11.
command=/usr/local/bin/logpipe.sh ops-proxy 12 1 "" /usr/local/bin/python3.11 -m uvicorn app:app --host 127.0.0.1 --port 8001
directory=/opt/ops-proxy
user=hindsight
autostart=true
autorestart=true
stopasgroup=true
killasgroup=true
startsecs=5
startretries=3
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
environment=LOGS_ADMIN_KEY="%(ENV_LOGS_ADMIN_KEY)s",LOGS_DIR="/data/logs"
```

Retention (≈ 432 MB total; tune freely): hindsight-api 60 MB × 6 = 360, others 12 MB × 2 = 24 each.

- [ ] **Step 3: Lint + commit**

```bash
python3 -c "import configparser; c=configparser.ConfigParser(strict=False, interpolation=None); c.read('supervisord.conf'); print('programs:', [s for s in c.sections() if s.startswith('program:')])"
git add supervisord.conf
git commit -m "Route web services through logpipe.sh; add ops-proxy program (durable /data logs)"
```
Expected: 4 programs listed (nginx, hindsight-api, control-plane, ops-proxy).

---

### Task 3: nginx — expose `/logs`

**File:** `nginx.conf` (this is the envsubst *template*; `$host` is preserved by the restricted envsubst in `start-nginx.sh`).

- [ ] **Step 1: Add the `opsproxy` upstream** (after the `controlplane` upstream block):

```nginx
    upstream opsproxy {
        server 127.0.0.1:8001;
    }
```

- [ ] **Step 2: Add the `/logs` location** (before the catch-all `location /`):

```nginx
        # Admin-gated durable log retrieval → ops-proxy
        location /logs {
            proxy_pass http://opsproxy;
            proxy_set_header Host $host;
        }
```

- [ ] **Step 3: Commit**

```bash
git add nginx.conf
git commit -m "nginx: add opsproxy upstream and route /logs to it"
```

---

### Task 4: Dockerfile — ship the artifacts + log dir

**File:** `Dockerfile` (insert before `USER hindsight`, after the existing COPY block at lines ~26-33).

- [ ] **Step 1: Add COPY + install + mkdir**

```dockerfile
# Durable volume logging: writer + supervisord wrapper, log dir on /data.
COPY logwriter/logwriter.py /opt/logwriter.py
COPY logpipe.sh /usr/local/bin/logpipe.sh
RUN chmod +x /usr/local/bin/logpipe.sh && mkdir -p /data/logs && chown -R hindsight:hindsight /data/logs

# Ops-proxy admin service (standalone FastAPI; serves GET /logs).
COPY ops-proxy/ /opt/ops-proxy/
RUN pip install --no-cache-dir -r /opt/ops-proxy/requirements.txt
```

- [ ] **Step 2: Commit**

```bash
git add Dockerfile
git commit -m "Dockerfile: ship logwriter.py + logpipe.sh + ops-proxy; create /data/logs"
```

---

### Task 5: appliku.yml — declare the secret

**File:** `appliku.yml`

- [ ] **Step 1: Add under `environment_variables:`** (e.g. after the `SYNC_STATE_FILE` entry):

```yaml
    # Admin key for the durable-log retrieval endpoint (GET /logs, ops-proxy).
    # Set the value in the Appliku dashboard (repo is public).
    - name: LOGS_ADMIN_KEY
      source: manual
```

- [ ] **Step 2: Validate + commit**

```bash
../hindsight-deploy/burrfect-proxy/.venv-test/bin/python -c "import yaml; d=yaml.safe_load(open('appliku.yml')); print('LOGS_ADMIN_KEY' in [e['name'] for e in d['build_settings']['environment_variables']])"
git add appliku.yml
git commit -m "Declare LOGS_ADMIN_KEY secret for ops-proxy /logs endpoint"
```
Expected: `True`.

---

### Task 6: Deploy + verify

- [ ] **Step 1: Orchestrator — set the dashboard secret.** Ask Dan to add `LOGS_ADMIN_KEY` to the Appliku **n8nhindsight** app env (generate with `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`), and keep the value for the verify curls.

- [ ] **Step 2: Push (auto-deploys master)**

```bash
git push origin master
```

- [ ] **Step 3: Confirm the deploy**

```bash
appliku deployments latest -t daniel-bennett-svtnoxta -a 4460
```
Expected: latest = the new commit, Status `Finished`.

- [ ] **Step 4: Service health + heartbeat-split**

```bash
appliku apps logs -t daniel-bennett-svtnoxta -a 4460 --tail 250 > /tmp/n8n_log.txt 2>&1
grep -c 'success: ops-proxy entered RUNNING' /tmp/n8n_log.txt   # >0
grep -c WORKER_STATS /tmp/n8n_log.txt                            # >0 (heartbeat still in live view)
grep -iE "ImportError|ModuleNotFound|Traceback|FATAL|exited:" /tmp/n8n_log.txt | head   # expect none
```

- [ ] **Step 5: Endpoint checks (keyless + with key)**

```bash
# keyless → 401
curl -s -o /dev/null -w "%{http_code}\n" "https://n8nhindsight.applikuapp.com/logs?service=hindsight-api"
# with key → 200 + recent lines, and NONE contain WORKER_STATS
curl -s "https://n8nhindsight.applikuapp.com/logs?service=hindsight-api&lines=20" -H "Authorization: Bearer $LOGS_ADMIN_KEY"
# rate limit → 30×200 then 429
for i in $(seq 1 35); do curl -s -o /dev/null -w "%{http_code} " "https://n8nhindsight.applikuapp.com/logs?service=hindsight-api&lines=1" -H "Authorization: Bearer $LOGS_ADMIN_KEY"; done; echo
```
Expected: `401`; then 20 lines with no `WORKER_STATS`; then thirty `200`s followed by `429`s.

- [ ] **Step 6: Retain the outcome to dan-shared** (bank_id=`dan-shared`): note that durable logging is now live on n8n-hindsight, mirroring hindsight-deploy.

---

## Self-Review

- **Coverage:** artifacts copied + tested (T1), capture-split for all 3 web programs + heartbeat drop on hindsight-api (T2), ops-proxy program (T2), `/logs` route (T3), image ships artifacts + installs slowapi (T4), secret declared (T5), deploy+verify incl. 429 (T6). ✅
- **n8n-specific gotchas captured:** envsubst-restricted nginx template (`$host` safe), system-python uvicorn, sibling test venv, cron one-offs out of scope, `/data` volume present. ✅
- **Placeholders:** none — all commands/content concrete (paths, app id 4460, domain, branch master). ✅
- **Name consistency:** lognames in T2 (`hindsight-api`, `ops-proxy`) match `service=` in T6 curls; `LOGS_ADMIN_KEY`/`LOGS_DIR` used identically in T2 env + ops-proxy app. ✅

> Note: the copied `logwriter`/`logpipe`/`ops-proxy` are battle-tested verbatim from hindsight-deploy (built + prod-verified 2026-06-04), so Task 1 runs their existing tests rather than re-deriving them via TDD. All *new* (wiring) work is config, verified at deploy in Task 6.
