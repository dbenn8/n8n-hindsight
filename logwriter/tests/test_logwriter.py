import importlib.util
import io
import pathlib

spec = importlib.util.spec_from_file_location(
    "logwriter", pathlib.Path(__file__).resolve().parents[1] / "logwriter.py")
logwriter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logwriter)


def test_echoes_every_line_to_stdout_but_filters_file(tmp_path):
    out = tmp_path / "x.log"
    echo = io.StringIO()
    logwriter.run(["keep one", "WORKER_STATS noise", "keep two"],
                  str(out), max_bytes=10_000, backups=2, drops=["WORKER_STATS"],
                  echo=echo)
    # stdout (Appliku live view) gets EVERYTHING, heartbeat included
    assert echo.getvalue().splitlines() == ["keep one", "WORKER_STATS noise", "keep two"]
    # durable file drops the heartbeat
    assert "WORKER_STATS" not in out.read_text()
    assert "keep one" in out.read_text() and "keep two" in out.read_text()


def test_drops_matching_lines(tmp_path):
    out = tmp_path / "x.log"
    logwriter.run(["keep one", "WORKER_STATS noise", "keep two"],
                  str(out), max_bytes=10_000, backups=2, drops=["WORKER_STATS"])
    text = out.read_text()
    assert "keep one" in text and "keep two" in text
    assert "WORKER_STATS" not in text


def test_passes_all_when_no_drop(tmp_path):
    out = tmp_path / "x.log"
    logwriter.run(["a", "b", "c"], str(out), max_bytes=10_000, backups=2, drops=[])
    assert out.read_text().splitlines() == ["a", "b", "c"]


def test_rotation_creates_capped_backups(tmp_path):
    out = tmp_path / "x.log"
    # each line ~46 bytes; max_bytes=100 forces frequent rotation
    lines = [f"line-{i:040d}" for i in range(50)]
    logwriter.run(lines, str(out), max_bytes=100, backups=2, drops=[])
    assert out.exists()
    assert (tmp_path / "x.log.1").exists()
    assert (tmp_path / "x.log.2").exists()
    # backupCount=2 -> never a .3
    assert not (tmp_path / "x.log.3").exists()


# --- query-text redaction (privacy): the API logs the first 50 chars of each
# recall/reflect query; logwriter must remove that text from BOTH outputs. ---

TS = "2026-11-19 15:15:25,040 - INFO - hindsight_api.engine.memory_engine - "


def _run(lines, tmp_path):
    out = tmp_path / "x.log"
    echo = io.StringIO()
    logwriter.run(lines, str(out), max_bytes=100_000, backups=1, drops=[], echo=echo)
    return echo.getvalue(), out.read_text()


def test_redacts_recall_start_query_in_stdout_and_file(tmp_path):
    echo, text = _run([TS + "[RECALL n8n] Starting recall for query: SECRETPROMPT how do I... (tags=a)"], tmp_path)
    for got in (echo, text):
        assert "SECRETPROMPT" not in got
        assert "[RECALL n8n] Starting recall for query: [redacted]" in got


def test_redacts_recall_query_line_and_keeps_budget_suffix(tmp_path):
    echo, text = _run(["[RECALL n8n-25086-0e20e8] Query: 'SECRETPROMPT it's a test...' (budget=mid, max_tokens=4096)"], tmp_path)
    for got in (echo, text):
        assert "SECRETPROMPT" not in got and "it's a test" not in got
        assert "[RECALL n8n-25086-0e20e8] Query: '[redacted]' (budget=mid, max_tokens=4096)" in got


def test_redacts_reflect_start_and_timeout_lines(tmp_path):
    echo, text = _run([
        TS + "[REFLECT ab12] Starting agentic reflect for query: SECRETPROMPT one...",
        TS + "[REFLECT ab12] Wall-clock timeout after 30.0s (limit: 30s) for query: SECRETPROMPT two...",
    ], tmp_path)
    for got in (echo, text):
        assert "SECRETPROMPT" not in got
        assert got.count("for query: [redacted]") == 2


def test_drops_continuation_lines_of_a_multiline_query(tmp_path):
    echo, text = _run([
        TS + "[RECALL n8n] Starting recall for query: SECRETPROMPT first line",
        "second line of the SECRETPROMPT",
        "third line... (tags=a)",
        "[RECALL n8n-25086-0e20e8] Query: 'SECRETPROMPT first line",
        "second line of the SECRETPROMPT...' (budget=mid, max_tokens=4096)",
        "  [1] Generate query embedding: 0.01s",
        TS + "next record",
    ], tmp_path)
    for got in (echo, text):
        assert "SECRETPROMPT" not in got and "second line" not in got and "third line" not in got
        assert "  [1] Generate query embedding: 0.01s" in got
        assert "next record" in got


def test_lines_without_query_text_are_unchanged(tmp_path):
    lines = [TS + "[RECALL n8n-1] Complete: 12 results", "10.0.0.1 - - [19/Nov/2026:15:15:25 +0000] \"POST /public/recall HTTP/1.1\" 200 12", "  [2] Parallel retrieval (4 fact_types)"]
    echo, text = _run(lines, tmp_path)
    assert echo.splitlines() == lines and text.splitlines() == lines


# --- multi-line queries whose later lines look like log lines (marker run d4a8d6b64856) ---

def test_drops_loglike_continuation_lines_until_the_query_terminator(tmp_path):
    echo, text = _run([
        TS + "[RECALL n8n-1] Starting recall for query: privacy test a",
        "ERROR: SECRETPROMPT line two",
        "...",
        TS + "next record",
        "[RECALL n8n-1] Query: 'privacy test a",
        "INFO: SECRETPROMPT line two",
        "...' (budget=300, max_tokens=4096)",
        "INFO:     after the query",
    ], tmp_path)
    for got in (echo, text):
        assert "SECRETPROMPT" not in got
        assert "next record" in got
        assert "INFO:     after the query" in got
        assert got.count("[redacted]") == 2


def test_terminator_with_tags_closes_a_multiline_query(tmp_path):
    echo, text = _run([
        TS + "[RECALL n8n-1] Starting recall for query: first line",
        "WARNING: SECRETPROMPT..., tags=['SECRETTAG'] (any)",
        "INFO:     kept line",
    ], tmp_path)
    for got in (echo, text):
        assert "SECRET" not in got
        assert "INFO:     kept line" in got


def test_single_line_query_does_not_drop_the_next_log_line(tmp_path):
    echo, text = _run([
        TS + "[RECALL n8n-1] Starting recall for query: SECRETPROMPT one line..., tags=['a'] (any)",
        "INFO:     kept line",
    ], tmp_path)
    for got in (echo, text):
        assert "SECRETPROMPT" not in got
        assert "INFO:     kept line" in got


def test_unterminated_query_stops_dropping_after_the_cap(tmp_path):
    lines = [TS + "[RECALL n8n-1] Starting recall for query: no terminator follows"]
    lines += [TS + f"record {i}" for i in range(60)]
    echo, text = _run(lines, tmp_path)
    for got in (echo, text):
        assert "record 59" in got
        assert "record 0\n" not in got
