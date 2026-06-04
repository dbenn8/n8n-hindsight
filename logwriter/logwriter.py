"""Read stdin; echo EVERY line to stdout (so supervisord/Appliku keep the full
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
        for raw in stream:
            line = raw.rstrip("\n")
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
