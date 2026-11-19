"""Remove query text from EXISTING log files (the log writer only covers new lines).

Dry run by default: prints counts only, never log content.
  python3 redact_files.py FILE...                      counts
  python3 redact_files.py --apply FILE...              rewrite through a temp file (rotated files)
  python3 redact_files.py --apply --in-place FILE...   rewrite the same inode (the live file,
                                                        which the log writer holds open in append mode)
Two passes:
  1. orphans: lines of a multi-line query that an earlier masking run left after
     an already redacted line, because they look like log lines. They are found
     by the end of the logged text ("..." plus tags or the budget suffix) within
     50 characters after the redacted line.
  2. logwriter.scrub(): the same masking as the live log writer.
"""
import argparse
import os
import re
import shutil
import sys

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), "/opt"]
import logwriter as lw  # noqa: E402

_DONE_FOR = re.compile(r"\bfor query: " + re.escape(lw.REDACTED))
_DONE_QUOTE = re.compile(r"\] Query: '" + re.escape(lw.REDACTED))
_END_FOR = re.compile(r"^(.*)\.\.\.(?:, tags=.*| \(.*\))?$")
_END_QUOTE = re.compile(r"^(.*)\.\.\.' \(budget=[\w.]+, max_tokens=\d+[^']*\)$")
_QUERY_CHARS = 50


def _orphan_span(lines, i):
    """Number of lines after lines[i] that are leftover query text (0 = none)."""
    end = _END_FOR if _DONE_FOR.search(lines[i]) else _END_QUOTE
    used = 0
    for n, s in enumerate(lines[i + 1:i + 1 + lw._OPEN_CAP], 1):
        if lw.redact(s)[1]:
            return 0  # the next query line: nothing was left behind
        m = end.match(s)
        if m and used + len(m.group(1)) <= _QUERY_CHARS:
            return n
        used += len(s) + 1
        if used > _QUERY_CHARS:
            return 0
    return 0


def clean(lines):
    """lines without trailing newline -> (clean lines, counts)."""
    stats = {"masked": 0, "dropped": 0, "orphans": 0}
    kept, i = [], 0
    while i < len(lines):
        kept.append(lines[i])
        if _DONE_FOR.search(lines[i]) or _DONE_QUOTE.search(lines[i]):
            n = _orphan_span(lines, i)
            stats["orphans"] += n
            i += n
        i += 1
    return list(lw.scrub(kept, stats)), stats


def process(path, apply, in_place):
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().split("\n")
    tail_newline = lines and lines[-1] == ""
    if tail_newline:
        lines.pop()
    out, stats = clean(lines)
    if apply and out != lines:
        data = "\n".join(out) + ("\n" if tail_newline else "")
        if in_place:
            with open(path, "r+", encoding="utf-8") as fh:
                fh.truncate(0)
                fh.write(data)
        else:
            tmp = path + ".tmp-redact"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            shutil.copystat(path, tmp)
            os.replace(tmp, path)
    print(f"{path} lines_in={len(lines)} lines_out={len(out)} masked={stats['masked']} "
          f"dropped={stats['dropped']} orphans={stats['orphans']} applied={bool(apply and out != lines)}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--in-place", action="store_true")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)
    for f in args.files:
        process(f, args.apply, args.in_place)


if __name__ == "__main__":
    main()
