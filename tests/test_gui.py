from __future__ import annotations

import json
import queue
import sys
import time
from pathlib import Path

import pytest

from chatlog_keeper import gui


ROOT = Path(__file__).resolve().parents[1]


def _drain(events: "queue.Queue[tuple]", timeout: float = 60.0):
    """Collect events until the run reports that it finished."""

    logs = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.2)
        except queue.Empty:
            continue
        if event[0] == "log":
            logs.append(event[1])
        elif event[0] == "finished":
            return logs, event[2], event[3]
    raise AssertionError("the child process did not finish in time")


def _python(code: str):
    return [sys.executable, "-c", code]


# ─── command line and environment ─────────────────────────────────────────────

def test_source_run_uses_the_cli_module(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert gui.cli_command(["probe"]) == [
        sys.executable, "-m", "chatlog_keeper.cli", "probe"
    ]


def test_frozen_run_restarts_the_same_executable(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert gui.cli_command(["probe"]) == [sys.executable, "probe"]


def test_arguments_match_the_cli_contract():
    assert gui.export_arguments("wechat", 30, "/tmp/out") == [
        "wechat", "--days", "30", "--out", "/tmp/out"
    ]
    assert gui.extract_key_arguments("qq", "passive") == [
        "extract-key", "--source", "qq", "--method", "passive"
    ]
    assert gui.images_arguments("/src", "/out") == [
        "images", "--src", "/src", "--out", "/out"
    ]


def test_chosen_data_folder_overrides_detection_only_for_that_client():
    env = gui.child_environment({"wechat": "/data/wx", "qq": None}, base={"A": "1"})
    assert env == {"A": "1", "CHATLOG_WECHAT_DATA_ROOT": "/data/wx"}


@pytest.mark.parametrize(
    "text, expected",
    [("30", 30), (" 7 ", 7), ("0", None), ("-3", None), ("abc", None),
     (str(gui.ALL_HISTORY_DAYS), gui.ALL_HISTORY_DAYS),
     (str(gui.ALL_HISTORY_DAYS + 1), None)],
)
def test_parse_days_accepts_only_a_sane_window(text, expected):
    assert gui.parse_days(text) == expected


def test_decode_output_falls_back_from_utf8():
    assert gui.decode_output("微信".encode("utf-8")) == "微信"
    # Bytes that are not valid UTF-8 must still come back as text.
    assert isinstance(gui.decode_output(b"\xce\xa2\xd0\xc5"), str)


# ─── reading CLI results ──────────────────────────────────────────────────────

def test_probe_states():
    ready = gui.source_status("wechat", {"available": True, "client_running": True})
    assert ready.state == gui.STATE_READY

    needs = gui.source_status("qq", {"available": False, "needs_key": True,
                                     "client_running": True})
    assert needs.state == gui.STATE_NEEDS_KEY
    assert "获取密钥" in needs.text

    closed = gui.source_status("wechat", {"available": False, "needs_key": True,
                                          "client_running": False})
    assert closed.state == gui.STATE_NEEDS_KEY
    assert "打开微信" in closed.text

    missing = gui.source_status("qq", {"available": False, "client_running": False})
    assert missing.state == gui.STATE_NOT_FOUND

    assert gui.source_status("qq", {"available": False, "error": "x"}).state == gui.STATE_ERROR
    assert gui.source_status("qq", None).state == gui.STATE_ERROR


def test_export_success_points_at_the_html():
    outcome = gui.export_outcome(
        "wechat",
        {"source": "wechat", "available": True, "n_messages": 12, "days": 30,
         "out_html": "/tmp/out/wechat_messages.html"},
        0,
    )
    assert outcome.ok and not outcome.empty
    assert outcome.html == Path("/tmp/out/wechat_messages.html")
    assert outcome.folder == Path("/tmp/out")
    assert "12" in outcome.text


def test_export_with_no_messages_is_reported_as_empty():
    outcome = gui.export_outcome(
        "qq",
        {"available": True, "n_messages": 0, "days": gui.ALL_HISTORY_DAYS,
         "out_html": "/tmp/out/qq_messages.html"},
        0,
    )
    assert outcome.ok and outcome.empty
    assert "全部记录" in outcome.text


@pytest.mark.parametrize("error", ["no_enc_keys", "no_key_or_db"])
def test_export_without_a_key_sends_the_user_to_key_extraction(error):
    outcome = gui.export_outcome("wechat", {"available": False, "error": error}, 1)
    assert not outcome.ok and outcome.needs_key
    assert "获取密钥" in outcome.text


def test_export_failures_never_claim_success():
    assert not gui.export_outcome("qq", {"available": False, "error": "export_failed"}, 1).ok
    assert not gui.export_outcome("qq", None, 2).ok
    assert not gui.export_outcome("qq", None, None).ok
    # ``available`` without an output file is not a finished export.
    assert not gui.export_outcome("qq", {"available": True}, 0).ok


def test_key_outcomes():
    assert gui.key_outcome("wechat", {"ok": True, "fresh_extraction": True}, 0).ok
    cached = gui.key_outcome("wechat", {"ok": True, "fresh_extraction": False}, 0)
    assert cached.ok and "保存好" in cached.text
    passive = gui.key_outcome("qq", {"ok": False, "method": "passive", "error": "x"}, 1)
    assert not passive.ok and "被动方式" in passive.text
    active = gui.key_outcome("qq", {"ok": False, "method": "active", "error": "boom"}, 1)
    assert not active.ok and "boom" in active.text
    denied = gui.key_outcome("qq", {"ok": False, "error": "process_access_denied"}, 1)
    assert "拒绝" in denied.text
    assert not gui.key_outcome("qq", None, 1).ok


def test_images_outcomes():
    done = gui.images_outcome({"available": True, "decrypted": 3, "failed": 1,
                               "out": "/tmp/img"}, 0)
    assert done.ok and done.folder == Path("/tmp/img")
    nothing = gui.images_outcome({"available": True, "decrypted": 0, "failed": 0,
                                  "out": "/tmp/img"}, 0)
    assert not nothing.ok
    assert not gui.images_outcome(None, 1).ok


def test_parse_result_rejects_anything_but_a_json_object():
    assert gui.parse_result('{"a": 1}') == {"a": 1}
    assert gui.parse_result("[1]") is None
    assert gui.parse_result("") is None
    assert gui.parse_result("Traceback ...") is None


# ─── the child process ────────────────────────────────────────────────────────

def test_run_reports_stderr_lines_and_the_final_stdout():
    events: "queue.Queue[tuple]" = queue.Queue()
    code = (
        "import sys, json;"
        "sys.stderr.write('warming up\\n'); sys.stderr.flush();"
        "print(json.dumps({'ok': True})); sys.exit(3)"
    )
    run = gui.CliRun(_python(code), gui.child_environment({}), events)
    run.start()
    logs, returncode, stdout_text = _drain(events)
    assert logs == ["warming up"]
    assert returncode == 3
    assert gui.parse_result(stdout_text) == {"ok": True}
    assert not run.cancelled


def test_cancel_stops_a_running_child():
    events: "queue.Queue[tuple]" = queue.Queue()
    run = gui.CliRun(
        _python("import time; print('started', flush=True); time.sleep(600)"),
        gui.child_environment({}),
        events,
    )
    run.start()
    time.sleep(1.0)
    started = time.monotonic()
    run.cancel()
    _logs, returncode, _stdout = _drain(events, timeout=30)
    assert time.monotonic() - started < 30
    assert run.cancelled
    assert returncode != 0


def test_a_program_that_cannot_start_is_reported_not_raised(tmp_path):
    events: "queue.Queue[tuple]" = queue.Queue()
    run = gui.CliRun([str(tmp_path / "missing-program")], gui.child_environment({}), events)
    run.start()
    logs, returncode, stdout_text = _drain(events)
    assert returncode is None and stdout_text == ""
    assert logs


def test_real_probe_through_the_same_path_the_window_uses():
    events: "queue.Queue[tuple]" = queue.Queue()
    env = gui.child_environment({})
    env["PYTHONPATH"] = str(ROOT)
    run = gui.CliRun(gui.cli_command(["probe"]), env, events)
    run.start()
    _logs, returncode, stdout_text = _drain(events, timeout=120)
    result = gui.parse_result(stdout_text)
    assert returncode == 0
    assert result is not None and set(result) == {"qq", "wechat"}
    for source in gui.SOURCES:
        assert gui.source_status(source, result[source]).state in {
            gui.STATE_READY, gui.STATE_NEEDS_KEY, gui.STATE_NOT_FOUND
        }


# ─── the window itself (needs Tk and a display) ───────────────────────────────

_FAKE_CLI = r"""
import json, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "probe":
    print(json.dumps({
        "qq": {"available": False, "needs_key": True, "client_running": True},
        "wechat": {"available": False, "client_running": False},
    }))
elif args[0] in ("qq", "wechat"):
    out = Path(args[args.index("--out") + 1])
    out.mkdir(parents=True, exist_ok=True)
    html = out / (args[0] + "_messages.html")
    html.write_text("<html></html>", encoding="utf-8")
    print(json.dumps({"available": True, "n_messages": 5,
                      "days": int(args[args.index("--days") + 1]),
                      "out_html": str(html)}))
"""


@pytest.fixture()
def window(tmp_path):
    if gui.tk is None:
        pytest.skip("this Python has no Tk")
    fake = tmp_path / "fake_cli.py"
    fake.write_text(_FAKE_CLI, encoding="utf-8")
    try:
        app = gui.create_app(
            command_factory=lambda arguments: [sys.executable, str(fake), *arguments],
            auto_probe=False,
            interactive=False,
        )
    except gui.tk.TclError:
        pytest.skip("no display available for Tk")
    yield app
    try:
        app.shutdown()
    except gui.tk.TclError:
        pass


def _run_until_idle(app, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if not app.busy:
            app.root.update()
            return
        time.sleep(0.02)
    raise AssertionError("the window stayed busy")


def test_window_shows_probe_results_and_exports(window, tmp_path):
    app = window
    app.probe()
    assert app.busy
    _run_until_idle(app)
    assert app.statuses["qq"].state == gui.STATE_NEEDS_KEY
    assert app.statuses["wechat"].state == gui.STATE_NOT_FOUND

    out = tmp_path / "导出"
    app.source_var.set("qq")
    app.range_var.set(gui.RANGE_PRESETS[0][0])
    app.out_var.set(str(out))
    app.export()
    _run_until_idle(app)
    assert (out / "qq_messages.html").is_file()
    assert "5" in app.status_var.get()
    assert str(app._open_html_button.cget("state")) == "normal"


def test_window_rejects_a_bad_custom_range_without_starting(window):
    app = window
    app.range_var.set(gui.CUSTOM_RANGE)
    app.days_var.set("0")
    app.export()
    assert not app.busy


# ─── packaging ────────────────────────────────────────────────────────────────

def test_gui_spec_reuses_the_cli_spec_preamble():
    base = (ROOT / "packaging" / "chatlog_keeper.spec").read_text(encoding="utf-8")
    spec = (ROOT / "packaging" / "chatlog_keeper_gui.spec").read_text(encoding="utf-8")
    # The window spec splits the command-line spec on this exact marker.
    assert base.count("\na = Analysis(") == 1
    assert '_GUI_SPLIT = "\\na = Analysis("' in spec
    assert 'os.path.join(SPEC_DIR, "chatlog_keeper_gui_main.py")' in spec
    assert 'name="chatlog-keeper-gui"' in spec
    assert "console=False" in spec
    assert '"tkinter"' not in spec  # the window needs Tk, unlike the CLI build


def test_gui_frozen_entry_dispatches_window_helper_and_cli():
    entry = (ROOT / "packaging" / "chatlog_keeper_gui_main.py").read_text(
        encoding="utf-8"
    )
    assert "chatlog_keeper.gui" in entry
    assert 'arguments[:1] == ["--_qq-sqlite-helper"]' in entry
    assert "chatlog_keeper._qq_sqlite_helper" in entry
    assert "from chatlog_keeper.cli import main" in entry


def test_selftest_report_is_written_even_without_tk(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "tk", None)
    report = tmp_path / "report.json"
    assert gui.main(["--gui-selftest", "--report", str(report)]) == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ok"] is False and payload["error"] == "RuntimeError"
