"""Read stdin; echo EVERY line (query text redacted, see redact()) to stdout (so supervisord/Appliku keep the full
live view, heartbeat included), and write the lines that do NOT match any --drop
regex to a size-rotated durable file. Pure stdlib; used by logpipe.sh.

logwriter is the LAST stage of the logpipe pipeline, so its stdout IS the wrapped
program's stdout (the supervisord capture pipe) — no `tee`/`/dev/stdout` needed."""
import argparse
import logging
import logging.handlers
import os
import re
import sys


# --- Query-text redaction (privacy) -----------------------------------------
# The Hindsight API logs the first 50 characters of each recall/reflect query:
#   "... Starting recall for query: <text>...<tags>"
#   "[RECALL <id>] Query: '<text>...' (budget=..., max_tokens=...)"
#   "... Starting agentic reflect for query: <text>..."
#   "... Wall-clock timeout ... for query: <text>..."
# That text is user prompt content. It must not reach stdout (Docker log) or the
# durable file, so redact() removes it from BOTH outputs. A query can hold line
# breaks; the lines that follow a redacted line are dropped until the next line
# with a known log shape.
# A later line of a query can itself look like a log line ("ERROR: ..."). The
# logged text always ends with "..." (plus tags or the budget suffix). So when
# the first line does not hold that end, every following line is dropped until
# the line that holds it (at most _OPEN_CAP lines: the text is 50 characters).
REDACTED = "[redacted]"
_FOR_QUERY = re.compile(r"\bfor query: ")
_QUERY_QUOTE = re.compile(r"\] Query: '")
_BUDGET_SUFFIX = re.compile(r"' \(budget=[\w.]+, max_tokens=\d+[^']*\)$")
_KNOWN_SHAPE = re.compile(
    r"^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} - "   # python logging record
    r"|\[[A-Z_]+ [^\]]*\] "                               # [RECALL id] / [REFLECT id]
    r"|  \["                                               # "  [1] ..." phase lines
    r"|\d{1,3}(?:\.\d{1,3}){3} - "                         # access-log line
    r"|[A-Z]+:\s)")                                        # uvicorn "INFO:   ..."


_FOR_QUERY_END = re.compile(r"\.\.\.(?:, tags=.*| \(.*\))?$")
_OPEN_CAP = 51


def query_open(line):
    """True when a query line does not hold the end of the logged query text."""
    m = _FOR_QUERY.search(line)
    if m:
        rest = line[m.end():]
        return not rest.startswith(REDACTED) and not _FOR_QUERY_END.search(rest)
    m = _QUERY_QUOTE.search(line)
    if m:
        rest = line[m.end():]
        return not rest.startswith(REDACTED) and not _BUDGET_SUFFIX.search(rest)
    return False


def query_end(line):
    """True when a continuation line holds the end of the logged query text."""
    return bool(_FOR_QUERY_END.search(line) or _BUDGET_SUFFIX.search(line))


def scrub(lines, stats=None):
    """Yield the lines without query text. `lines` have no trailing newline."""
    in_query = False  # True while the lines of a multi-line query continue
    open_left = 0     # >0: drop every line until the end of the query text
    for line in lines:
        opened = query_open(line)
        new, hit = redact(line)
        if hit:
            if stats is not None and new != line:
                stats["masked"] += 1
            in_query = True
            open_left = _OPEN_CAP if opened else 0
            yield new
            continue
        if open_left:
            open_left = 0 if query_end(line) else open_left - 1
            if stats is not None:
                stats["dropped"] += 1
            continue
        if in_query:
            if not _KNOWN_SHAPE.match(line):
                if stats is not None:
                    stats["dropped"] += 1
                continue  # continuation of the redacted query text
            in_query = False
        yield line


def redact(line):
    """Return (line_without_query_text, was_redacted)."""
    m = _FOR_QUERY.search(line)
    if m:
        return line[:m.end()] + REDACTED, True
    m = _QUERY_QUOTE.search(line)
    if m:
        cut = line.rfind("' (budget=")
        suffix = line[cut:] if cut >= m.end() and _BUDGET_SUFFIX.match(line[cut:]) else "'"
        return line[:m.end()] + REDACTED + suffix, True
    return line, False


def run(stream, out, max_bytes, backups, drops, echo=None):
    if echo is None:
        echo = sys.stdout
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    patterns = [re.compile(d) for d in drops]
    handler = logging.handlers.RotatingFileHandler(
        out, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("logwriter:" + out)
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        # 0) privacy: scrub() removes query text before a line goes anywhere
        for line in scrub(raw.rstrip("\n") for raw in stream):
            # 1) echo everything to stdout (Appliku live view, heartbeat included)
            echo.write(line + "\n")
            echo.flush()
            # 2) durable file gets everything EXCEPT dropped patterns
            if any(p.search(line) for p in patterns):
                continue
            log.info(line)
            handler.flush()
    finally:
        handler.close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-mb", type=int, default=25)
    ap.add_argument("--backups", type=int, default=1)
    ap.add_argument("--drop", action="append", default=[])
    args = ap.parse_args(argv)
    # readline-iter avoids stdin readahead buffering so lines flush promptly
    run(iter(sys.stdin.readline, ""), args.out,
        args.max_mb * 1024 * 1024, args.backups, args.drop)


if __name__ == "__main__":
    main()
