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
