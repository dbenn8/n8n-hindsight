import importlib.util
import pathlib

_DIR = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("redact_files", _DIR / "redact_files.py")
redact_files = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redact_files)

TS = "2026-11-19 20:10:16,343 - INFO - hindsight_api.engine.memory_engine - "


def test_removes_scraps_left_after_an_already_redacted_line():
    lines = [
        TS + "[RECALL n8n-1] Starting recall for query: [redacted]",
        "ERROR: SECRETPROMPT line two",
        "...",
        TS + "next record",
        "[RECALL n8n-1] Query: '[redacted]'",
        "ERROR: SECRETPROMPT line two",
        "...' (budget=300, max_tokens=4096)",
        "INFO:     after",
    ]
    out, stats = redact_files.clean(lines)
    assert "SECRETPROMPT" not in "\n".join(out)
    assert out == [lines[0], lines[3], lines[4], lines[7]]
    assert stats["orphans"] == 4


def test_keeps_normal_lines_after_an_already_redacted_line():
    lines = [
        TS + "[RECALL n8n-1] Starting recall for query: [redacted]",
        TS + "Loading the reranker model now...",
        "INFO:     1.2.3.4:5 - \"POST /x HTTP/1.1\" 200 OK",
    ]
    out, stats = redact_files.clean(lines)
    assert out == lines
    assert stats == {"masked": 0, "dropped": 0, "orphans": 0}


def test_masks_fresh_query_lines_like_the_log_writer():
    lines = [
        TS + "[RECALL n8n-1] Starting recall for query: SECRETPROMPT first",
        "ERROR: SECRETPROMPT two...",
        TS + "kept",
    ]
    out, stats = redact_files.clean(lines)
    assert "SECRETPROMPT" not in "\n".join(out)
    assert out[-1] == TS + "kept"
    assert stats["masked"] == 1 and stats["dropped"] == 1


def test_apply_rewrites_the_file_and_dry_run_does_not(tmp_path):
    f = tmp_path / "a.log"
    body = TS + "[RECALL n8n-1] Starting recall for query: [redacted]\nERROR: SECRETPROMPT\n...\n" + TS + "kept\n"
    f.write_text(body)
    redact_files.main([str(f)])
    assert f.read_text() == body
    redact_files.main(["--apply", "--in-place", str(f)])
    assert "SECRETPROMPT" not in f.read_text()
    assert f.read_text().endswith(TS + "kept\n")
