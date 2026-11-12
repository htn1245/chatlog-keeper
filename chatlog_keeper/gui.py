"""Desktop window for chatlog-keeper.

The window is a thin front end.  Every action starts the ordinary command-line
program in a child process and reads the JSON result it prints, so no export,
decryption or key logic is duplicated here and nothing in this module opens a
network connection.

Run it from a source checkout with ``python -m chatlog_keeper.gui``; the frozen
``chatlog-keeper-gui`` executable opens it when started without arguments.
"""

from __future__ import annotations

import json
import locale
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from chatlog_keeper import __version__

try:  # The helpers below stay importable on a Python built without Tk.
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - depends on the interpreter build
    tk = None  # type: ignore[assignment]


SOURCES = ("wechat", "qq")
SOURCE_LABELS = {"wechat": "微信", "qq": "QQ"}
DATA_ROOT_ENV = {
    "wechat": "CHATLOG_WECHAT_DATA_ROOT",
    "qq": "CHATLOG_QQ_DATA_ROOT",
}

# "Everything" is a look-back window long enough to predate both clients.
ALL_HISTORY_DAYS = 20000
RANGE_PRESETS = (
    ("最近 7 天", 7),
    ("最近 30 天", 30),
    ("最近 90 天", 90),
    ("最近一年", 365),
    ("全部记录（可能很慢）", ALL_HISTORY_DAYS),
)
CUSTOM_RANGE = "自定义天数"

STATE_READY = "ready"
STATE_NEEDS_KEY = "needs_key"
STATE_NOT_FOUND = "not_found"
STATE_ERROR = "error"

_STATE_COLORS = {
    STATE_READY: "#2e9e4f",
    STATE_NEEDS_KEY: "#d98a00",
    STATE_NOT_FOUND: "#9a9a9a",
    STATE_ERROR: "#d13438",
}
_NO_KEY_ERRORS = frozenset({"no_enc_keys", "no_key_or_db"})


# ─── command line of the child process ─────────────────────────────────────────

def cli_command(arguments: Sequence[str]) -> List[str]:
    """Return the command that runs the chatlog-keeper CLI with ``arguments``."""

    if getattr(sys, "frozen", False):
        # The frozen entry point dispatches to the CLI when given arguments.
        return [sys.executable, *arguments]
    return [sys.executable, "-m", "chatlog_keeper.cli", *arguments]


def export_arguments(source: str, days: int, out_dir: str) -> List[str]:
    return [source, "--days", str(int(days)), "--out", str(out_dir)]


def extract_key_arguments(source: str, method: str) -> List[str]:
    return ["extract-key", "--source", source, "--method", method]


def images_arguments(src_dir: str, out_dir: str) -> List[str]:
    return ["images", "--src", str(src_dir), "--out", str(out_dir)]


def child_environment(
    data_roots: Mapping[str, Optional[str]],
    base: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Environment for the child; a chosen data folder overrides auto-detection."""

    env = dict(os.environ if base is None else base)
    for source, variable in DATA_ROOT_ENV.items():
        chosen = data_roots.get(source)
        if chosen:
            env[variable] = str(chosen)
    return env


def default_output_dir() -> Path:
    home = Path.home()
    documents = home / "Documents"
    return (documents if documents.is_dir() else home) / "聊天记录导出"


def parse_days(text: str) -> Optional[int]:
    try:
        days = int(str(text).strip())
    except ValueError:
        return None
    return days if 1 <= days <= ALL_HISTORY_DAYS else None


# ─── reading what the CLI printed ─────────────────────────────────────────────

def parse_result(stdout_text: str) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(stdout_text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def decode_output(raw: bytes) -> str:
    """Decode child output: UTF-8 when valid, else the system code page."""

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False) or "utf-8", "replace")


def format_elapsed(seconds: float) -> str:
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole} 秒"
    return f"{whole // 60} 分 {whole % 60:02d} 秒"


def range_text(days: int) -> str:
    return "全部记录" if days >= ALL_HISTORY_DAYS else f"最近 {days} 天"


@dataclass(frozen=True)
class SourceStatus:
    source: str
    state: str
    client_running: bool
    text: str


def source_status(source: str, payload: Any) -> SourceStatus:
    """Turn one ``probe`` entry into the line shown next to the client name."""

    label = SOURCE_LABELS[source]
    if not isinstance(payload, dict) or payload.get("error"):
        return SourceStatus(source, STATE_ERROR, False, "检测时出错，请点“重新检测”")
    running = bool(payload.get("client_running"))
    if payload.get("available"):
        return SourceStatus(source, STATE_READY, running, "已就绪，可以直接导出")
    if payload.get("needs_key"):
        if running:
            text = "找到了聊天数据，还没有密钥，请点“获取密钥”"
        else:
            text = f"找到了聊天数据，还没有密钥：先登录并打开{label}，再点“获取密钥”"
        return SourceStatus(source, STATE_NEEDS_KEY, running, text)
    if running:
        text = f"{label}正在运行，但没找到它的聊天数据（可在“高级”菜单里指定文件夹）"
    else:
        text = f"没找到这台电脑上的{label}聊天数据"
    return SourceStatus(source, STATE_NOT_FOUND, running, text)


@dataclass(frozen=True)
class Outcome:
    ok: bool
    text: str
    html: Optional[Path] = None
    folder: Optional[Path] = None
    needs_key: bool = False
    empty: bool = False


def _no_result_text(action: str, returncode: Optional[int]) -> str:
    if returncode is None:
        return f"{action}失败：程序没有启动起来。"
    return f"{action}失败：程序没有返回结果（退出码 {returncode}）。"


def export_outcome(
    source: str,
    result: Optional[Dict[str, Any]],
    returncode: Optional[int],
) -> Outcome:
    label = SOURCE_LABELS[source]
    if result is None:
        return Outcome(False, _no_result_text("导出", returncode))
    error = str(result.get("error") or "")
    if result.get("available") and not error and result.get("out_html"):
        html = Path(str(result["out_html"]))
        count = int(result.get("n_messages") or 0)
        span = range_text(int(result.get("days") or 0))
        if count == 0:
            text = f"导出完成，但{span}内没有{label}消息，可以把时间范围选大一些再试。"
        else:
            text = f"导出完成：{span}共 {count} 条{label}消息。"
        return Outcome(True, text, html=html, folder=html.parent, empty=count == 0)
    if error in _NO_KEY_ERRORS:
        return Outcome(
            False,
            f"没有拿到{label}的解密密钥。请确认{label}已登录并正在运行，"
            "先点“获取密钥”，成功后再导出。",
            needs_key=True,
        )
    if error == "export_failed":
        return Outcome(False, f"导出失败：读取{label}聊天数据时出错。")
    return Outcome(False, f"导出失败：{error or '原因未知'}")


def key_outcome(
    source: str,
    result: Optional[Dict[str, Any]],
    returncode: Optional[int],
) -> Outcome:
    label = SOURCE_LABELS[source]
    if result is None:
        return Outcome(False, _no_result_text("获取密钥", returncode))
    if result.get("ok"):
        if result.get("fresh_extraction") is False:
            return Outcome(True, f"{label}已有保存好的可用密钥，可以直接导出。")
        return Outcome(True, f"{label}密钥已获取并保存，现在可以导出了。")
    error = str(result.get("error") or "")
    if error == "process_access_denied":
        return Outcome(False, f"系统拒绝读取{label}进程，没有取到密钥。")
    if str(result.get("method") or "") == "passive":
        return Outcome(False, f"被动方式没有取到{label}密钥。")
    return Outcome(False, f"主动方式没有取到{label}密钥：{error or '原因未知'}")


def images_outcome(
    result: Optional[Dict[str, Any]],
    returncode: Optional[int],
) -> Outcome:
    if result is None:
        return Outcome(False, _no_result_text("解密图片", returncode))
    if not result.get("available") or result.get("error"):
        return Outcome(False, f"解密图片失败：{result.get('error') or '原因未知'}")
    done = int(result.get("decrypted") or 0)
    failed = int(result.get("failed") or 0)
    folder = Path(str(result["out"])) if result.get("out") else None
    if done == 0 and failed == 0:
        return Outcome(False, "所选文件夹里没有找到 .dat 图片文件。", folder=folder)
    return Outcome(
        done > 0,
        f"图片解密完成：成功 {done} 张，失败 {failed} 张。",
        folder=folder,
    )


# ─── running the child process ────────────────────────────────────────────────

def _spawn_options() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def _terminate_tree(process: "subprocess.Popen[bytes]") -> None:
    """Stop the child and whatever it started (a frozen child has children)."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"
        )
        try:
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                **_spawn_options(),
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass


class CliRun:
    """One CLI child process, reported through a thread-safe event queue.

    Events are ``("log", text)`` for each line the child writes to stderr and
    one final ``("finished", run, returncode, stdout_text)``; ``returncode`` is
    ``None`` when the process could not be started.
    """

    def __init__(
        self,
        command: Sequence[str],
        env: Mapping[str, str],
        events: "queue.Queue[tuple]",
    ) -> None:
        self.command = list(command)
        self.env = dict(env)
        self.events = events
        self.cancelled = False
        self._process: Optional["subprocess.Popen[bytes]"] = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._work, name="chatlog-keeper-cli", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            process = self._process
        if process is not None:
            _terminate_tree(process)

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)

    def _work(self) -> None:
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                **_spawn_options(),
            )
        except OSError as exc:
            self.events.put(("log", f"无法启动程序：{type(exc).__name__}"))
            self.events.put(("finished", self, None, ""))
            return
        with self._lock:
            self._process = process
            cancelled = self.cancelled
        if cancelled:
            _terminate_tree(process)

        def forward_stderr() -> None:
            assert process.stderr is not None
            for raw in process.stderr:
                line = decode_output(raw).rstrip()
                if line:
                    self.events.put(("log", line))

        reader = threading.Thread(
            target=forward_stderr, name="chatlog-keeper-cli-stderr", daemon=True
        )
        reader.start()
        assert process.stdout is not None
        stdout_text = decode_output(process.stdout.read())
        returncode = process.wait()
        reader.join(timeout=5)
        self.events.put(("finished", self, returncode, stdout_text))


def open_path(path: Path) -> None:
    """Open a file or folder with the system's default program."""

    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ─── the window ───────────────────────────────────────────────────────────────

@dataclass
class _Task:
    kind: str  # probe / export / key / images
    title: str
    arguments: List[str]
    source: str = ""
    method: str = ""


HELP_TEXT = (
    "1. 在这台电脑上登录并打开你自己的微信或 QQ。\n"
    "2. 看“本机状态”：显示“已就绪”就可以直接导出；"
    "显示“还没有密钥”就先点右边的“获取密钥”。\n"
    "3. 选好来源、时间范围和保存位置，点“开始导出”。\n"
    "4. 导出完成后点“打开聊天记录”，会用浏览器打开一个网页，可以像翻聊天记录一样回看。\n\n"
    "全部操作只读取这台电脑上的本地数据，不联网、不上传。"
)


class App:
    POLL_MS = 80

    def __init__(
        self,
        root: "tk.Tk",
        command_factory: Callable[[Sequence[str]], List[str]] = cli_command,
        auto_probe: bool = True,
        interactive: bool = True,
    ) -> None:
        self.root = root
        self.interactive = interactive
        self._command_factory = command_factory
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._run: Optional[CliRun] = None
        self._task: Optional[_Task] = None
        self._started_at = 0.0
        self._data_roots: Dict[str, Optional[str]] = {}
        self._last_html: Optional[Path] = None
        self._last_folder: Optional[Path] = None
        self.statuses: Dict[str, SourceStatus] = {}
        self.probe_count = 0
        self._busy_widgets: List[Any] = []

        self._build_window()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(self.POLL_MS, self._poll)
        if auto_probe:
            root.after(150, self.probe)

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_window(self) -> None:
        root = self.root
        root.title(f"chatlog-keeper 聊天记录导出  v{__version__}")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self._build_menu()

        outer = ttk.Frame(root, padding=14)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        heading_font = _derived_font(root, size_delta=4, bold=True)
        ttk.Label(outer, text="导出你自己的微信 / QQ 聊天记录", font=heading_font).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            outer,
            text="只读取这台电脑上的本地数据，不联网、不上传。",
            foreground="#666666",
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        self._build_status(outer).grid(row=2, column=0, sticky="ew")
        self._build_tabs(outer).grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self._build_run_area(outer).grid(row=4, column=0, sticky="nsew", pady=(10, 0))

        root.update_idletasks()
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        advanced = tk.Menu(menubar, tearoff=False)
        for source in SOURCES:
            advanced.add_command(
                label=f"指定{SOURCE_LABELS[source]}数据文件夹…",
                command=lambda s=source: self._choose_data_root(s),
            )
        advanced.add_separator()
        advanced.add_command(label="恢复自动查找", command=self._reset_data_roots)
        menubar.add_cascade(label="高级", menu=advanced)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="使用说明", command=self._show_help)
        help_menu.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.root.configure(menu=menubar)

    def _build_status(self, parent: Any) -> Any:
        box = ttk.LabelFrame(parent, text="本机状态", padding=10)
        box.columnconfigure(2, weight=1)
        self._status_dots: Dict[str, Any] = {}
        self._status_texts: Dict[str, Any] = {}
        for row, source in enumerate(SOURCES):
            ttk.Label(box, text=SOURCE_LABELS[source], width=5).grid(
                row=row, column=0, sticky="w", pady=3
            )
            dot = tk.Label(box, text="●", foreground=_STATE_COLORS[STATE_NOT_FOUND])
            dot.grid(row=row, column=1, sticky="w", padx=(0, 6))
            text_var = tk.StringVar(value="正在检测…")
            text_label = ttk.Label(
                box, textvariable=text_var, wraplength=430, justify="left"
            )
            text_label.grid(row=row, column=2, sticky="ew")
            _wrap_to_width(text_label)
            button = ttk.Button(
                box,
                text="获取密钥",
                command=lambda s=source: self.extract_key(s, "passive"),
            )
            button.grid(row=row, column=3, sticky="e", padx=(10, 0), pady=2)
            self._busy_widgets.append(button)
            self._status_dots[source] = dot
            self._status_texts[source] = text_var
        recheck = ttk.Button(box, text="重新检测", command=self.probe)
        recheck.grid(row=len(SOURCES), column=3, sticky="e", padx=(10, 0), pady=(8, 0))
        self._busy_widgets.append(recheck)
        return box

    def _build_tabs(self, parent: Any) -> Any:
        notebook = ttk.Notebook(parent)

        export = ttk.Frame(notebook, padding=12)
        export.columnconfigure(1, weight=1)
        notebook.add(export, text="导出聊天记录")

        ttk.Label(export, text="来源").grid(
            row=0, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        self.source_var = tk.StringVar(value="wechat")
        source_row = ttk.Frame(export)
        source_row.grid(row=0, column=1, columnspan=2, sticky="w")
        for source in SOURCES:
            ttk.Radiobutton(
                source_row,
                text=SOURCE_LABELS[source],
                value=source,
                variable=self.source_var,
            ).pack(side="left", padx=(0, 16))

        ttk.Label(export, text="时间范围").grid(
            row=1, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        range_row = ttk.Frame(export)
        range_row.grid(row=1, column=1, columnspan=2, sticky="w")
        self.range_var = tk.StringVar(value=RANGE_PRESETS[1][0])
        range_box = ttk.Combobox(
            range_row,
            textvariable=self.range_var,
            values=[name for name, _days in RANGE_PRESETS] + [CUSTOM_RANGE],
            state="readonly",
            width=22,
        )
        range_box.pack(side="left")
        range_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_range())
        self.days_var = tk.StringVar(value="30")
        self._days_entry = ttk.Spinbox(
            range_row, from_=1, to=ALL_HISTORY_DAYS, textvariable=self.days_var, width=7
        )
        self._days_suffix = ttk.Label(range_row, text="天")

        ttk.Label(export, text="保存到").grid(
            row=2, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        self.out_var = tk.StringVar(value=str(default_output_dir()))
        ttk.Entry(export, textvariable=self.out_var).grid(
            row=2, column=1, sticky="ew", padx=(0, 6)
        )
        ttk.Button(
            export, text="选择…", command=lambda: self._choose_folder(self.out_var)
        ).grid(row=2, column=2, sticky="e")

        self._export_button = ttk.Button(export, text="开始导出", command=self.export)
        self._export_button.grid(row=3, column=1, columnspan=2, sticky="e", pady=(10, 0))
        self._busy_widgets.append(self._export_button)

        images = ttk.Frame(notebook, padding=12)
        images.columnconfigure(1, weight=1)
        notebook.add(images, text="解密微信图片")
        ttk.Label(
            images,
            text="把微信保存在本机的 .dat 图片文件解密成 jpg / png。",
            foreground="#666666",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(images, text="图片文件夹").grid(
            row=1, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        self.images_src_var = tk.StringVar()
        ttk.Entry(images, textvariable=self.images_src_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 6)
        )
        ttk.Button(
            images,
            text="选择…",
            command=lambda: self._choose_folder(self.images_src_var),
        ).grid(row=1, column=2, sticky="e")
        ttk.Label(images, text="保存到").grid(
            row=2, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        self.images_out_var = tk.StringVar(value=str(default_output_dir() / "图片"))
        ttk.Entry(images, textvariable=self.images_out_var).grid(
            row=2, column=1, sticky="ew", padx=(0, 6)
        )
        ttk.Button(
            images,
            text="选择…",
            command=lambda: self._choose_folder(self.images_out_var),
        ).grid(row=2, column=2, sticky="e")
        images_button = ttk.Button(images, text="开始解密", command=self.decrypt_images)
        images_button.grid(row=3, column=1, columnspan=2, sticky="e", pady=(10, 0))
        self._busy_widgets.append(images_button)
        return notebook

    def _build_run_area(self, parent: Any) -> Any:
        box = ttk.LabelFrame(parent, text="运行情况", padding=10)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(2, weight=1)

        top = ttk.Frame(box)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="准备好了")
        status_label = ttk.Label(
            top, textvariable=self.status_var, wraplength=520, justify="left"
        )
        status_label.grid(row=0, column=0, sticky="ew")
        _wrap_to_width(status_label)
        self._cancel_button = ttk.Button(
            top, text="取消", command=self.cancel, state="disabled"
        )
        self._cancel_button.grid(row=0, column=1, sticky="e", padx=(10, 0))

        self._progress = ttk.Progressbar(box, mode="determinate", value=0)
        self._progress.grid(row=1, column=0, sticky="ew", pady=(6, 6))

        log_frame = ttk.Frame(box)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self._log = tk.Text(
            log_frame,
            height=7,
            wrap="word",
            state="disabled",
            relief="solid",
            borderwidth=1,
            font=_derived_font(self.root, size_delta=-1),
        )
        self._log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self._log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._log.configure(yscrollcommand=scrollbar.set)

        actions = ttk.Frame(box)
        actions.grid(row=3, column=0, sticky="e", pady=(8, 0))
        self._open_html_button = ttk.Button(
            actions, text="打开聊天记录", command=self._open_html, state="disabled"
        )
        self._open_html_button.pack(side="left", padx=(0, 6))
        self._open_folder_button = ttk.Button(
            actions, text="打开所在文件夹", command=self._open_folder, state="disabled"
        )
        self._open_folder_button.pack(side="left")
        return box

    # ── small helpers ─────────────────────────────────────────────────────────

    def _sync_range(self) -> None:
        if self.range_var.get() == CUSTOM_RANGE:
            self._days_entry.pack(side="left", padx=(8, 4))
            self._days_suffix.pack(side="left")
        else:
            self._days_entry.pack_forget()
            self._days_suffix.pack_forget()

    def selected_days(self) -> Optional[int]:
        chosen = self.range_var.get()
        for name, days in RANGE_PRESETS:
            if name == chosen:
                return days
        return parse_days(self.days_var.get())

    def _choose_folder(self, variable: Any) -> None:
        current = variable.get().strip()
        initial = current if current and Path(current).is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(parent=self.root, initialdir=initial)
        if chosen:
            variable.set(str(Path(chosen)))

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log.configure(state="normal")
        self._log.insert("end", f"[{stamp}] {text}\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_busy(self, busy: bool) -> None:
        for widget in self._busy_widgets:
            widget.configure(state="disabled" if busy else "normal")
        self._cancel_button.configure(state="normal" if busy else "disabled")
        if busy:
            self._progress.configure(mode="indeterminate")
            self._progress.start(12)
        else:
            # An idle indeterminate bar still draws its block; show an empty one.
            self._progress.stop()
            self._progress.configure(mode="determinate", value=0)

    @property
    def busy(self) -> bool:
        return self._task is not None

    def _warn(self, title: str, text: str) -> None:
        if self.interactive:
            messagebox.showwarning(title, text, parent=self.root)

    def _ask(self, title: str, text: str, unattended: bool = False) -> bool:
        """Ask a yes/no question; ``unattended`` is the answer without a user."""

        if not self.interactive:
            return unattended
        return bool(messagebox.askyesno(title, text, parent=self.root))

    # ── actions ───────────────────────────────────────────────────────────────

    def _start(self, task: _Task) -> None:
        if self.busy:
            return
        self._task = task
        self._started_at = time.monotonic()
        self._set_busy(True)
        self._set_status(task.title)
        self.log(task.title)
        self._run = CliRun(
            self._command_factory(task.arguments),
            child_environment(self._data_roots),
            self._events,
        )
        self._run.start()

    def probe(self) -> None:
        self._start(_Task("probe", "正在检测本机的微信和 QQ…", ["probe"]))

    def extract_key(self, source: str, method: str) -> None:
        label = SOURCE_LABELS[source]
        if method == "active":
            title = f"正在用主动方式获取{label}密钥；如果弹出登录窗口，请在里面完成登录…"
        else:
            title = f"正在读取{label}密钥（请保持{label}已登录并在运行）…"
        self._start(
            _Task("key", title, extract_key_arguments(source, method), source, method)
        )

    def export(self) -> None:
        source = self.source_var.get()
        label = SOURCE_LABELS[source]
        days = self.selected_days()
        if days is None:
            self._warn("时间范围", f"请填写 1 到 {ALL_HISTORY_DAYS} 之间的天数。")
            return
        out_text = self.out_var.get().strip()
        if not out_text:
            self._warn("保存位置", "请先选择保存到哪个文件夹。")
            return
        out_dir = Path(out_text).expanduser()
        previous = out_dir / f"{source}_messages.html"
        if previous.exists() and not self._ask(
            "覆盖上次的导出",
            f"这个文件夹里已经有一份{label}聊天记录：\n{previous}\n\n"
            "继续导出会覆盖它。要继续吗？",
            unattended=True,
        ):
            return
        self._start(
            _Task(
                "export",
                f"正在导出{label}聊天记录（{range_text(days)}），数据多的话要等几分钟…",
                export_arguments(source, days, str(out_dir)),
                source,
            )
        )

    def decrypt_images(self) -> None:
        src = self.images_src_var.get().strip()
        out = self.images_out_var.get().strip()
        if not src or not Path(src).is_dir():
            self._warn("图片文件夹", "请先选择存放微信 .dat 图片的文件夹。")
            return
        if not out:
            self._warn("保存位置", "请先选择解密后的图片保存到哪个文件夹。")
            return
        self._start(
            _Task(
                "images",
                "正在解密微信图片…",
                images_arguments(src, str(Path(out).expanduser())),
            )
        )

    def cancel(self) -> None:
        if self._run is not None:
            self._set_status("正在取消…")
            self._run.cancel()

    # ── results ───────────────────────────────────────────────────────────────

    def _poll(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                if event[0] == "log":
                    self.log(event[1])
                elif event[0] == "finished" and event[1] is self._run:
                    self._finish(event[2], event[3])
        except queue.Empty:
            pass
        if self._task is not None and self._run is not None and not self._run.cancelled:
            elapsed = format_elapsed(time.monotonic() - self._started_at)
            self._set_status(f"{self._task.title}  已用时 {elapsed}")
        self.root.after(self.POLL_MS, self._poll)

    def _finish(self, returncode: Optional[int], stdout_text: str) -> None:
        task, run = self._task, self._run
        self._task = None
        self._run = None
        self._set_busy(False)
        if task is None or run is None:
            return
        if run.cancelled:
            self._set_status("已取消")
            self.log("已取消")
            return
        result = parse_result(stdout_text)
        getattr(self, f"_done_{task.kind}")(task, result, returncode)

    def _show_outcome(self, outcome: Outcome) -> None:
        self._set_status(outcome.text)
        self.log(outcome.text)

    def _done_probe(
        self, _task: _Task, result: Optional[Dict[str, Any]], _returncode: Optional[int]
    ) -> None:
        self.probe_count += 1
        ready = []
        for source in SOURCES:
            status = source_status(source, (result or {}).get(source))
            self.statuses[source] = status
            self._status_dots[source].configure(foreground=_STATE_COLORS[status.state])
            self._status_texts[source].set(status.text)
            if status.state == STATE_READY:
                ready.append(SOURCE_LABELS[source])
        if result is None:
            self._set_status("检测失败，请点“重新检测”。")
        elif ready:
            self._set_status(f"检测完成：{'、'.join(ready)}可以直接导出。")
        else:
            self._set_status("检测完成，请看上面“本机状态”里的提示。")
        self.log(self.status_var.get())
        if self.probe_count == 1 and self.statuses[self.source_var.get()].state != STATE_READY:
            # First look at this machine: preselect a client that can be exported.
            for source in SOURCES:
                if self.statuses[source].state == STATE_READY:
                    self.source_var.set(source)
                    break

    def _done_export(
        self, task: _Task, result: Optional[Dict[str, Any]], returncode: Optional[int]
    ) -> None:
        outcome = export_outcome(task.source, result, returncode)
        self._show_outcome(outcome)
        if outcome.ok:
            self._last_html, self._last_folder = outcome.html, outcome.folder
            self._open_html_button.configure(state="normal")
            self._open_folder_button.configure(state="normal")
            if not outcome.empty and self._ask(
                "导出完成", f"{outcome.text}\n\n现在打开聊天记录吗？"
            ):
                self._open_html()
        elif outcome.needs_key:
            self._warn("还没有密钥", outcome.text)
        else:
            self._warn("导出失败", outcome.text)

    def _done_key(
        self, task: _Task, result: Optional[Dict[str, Any]], returncode: Optional[int]
    ) -> None:
        outcome = key_outcome(task.source, result, returncode)
        self._show_outcome(outcome)
        if outcome.ok:
            self.probe()
            return
        label = SOURCE_LABELS[task.source]
        if task.method == "passive" and result is not None:
            if self._ask(
                "改用主动方式？",
                f"被动方式没有取到{label}密钥，新版{label}上比较常见。\n\n"
                f"可以改用“主动方式”：程序会另外启动一个{label}客户端并读取密钥，"
                "可能需要你在弹出的窗口里登录或扫码。\n"
                "按项目说明，主动方式的封号风险为“中–偏高”，被动方式为“低”。\n\n"
                "要改用主动方式吗？",
            ):
                self.extract_key(task.source, "active")
            return
        self._warn("没有取到密钥", outcome.text)

    def _done_images(
        self, _task: _Task, result: Optional[Dict[str, Any]], returncode: Optional[int]
    ) -> None:
        outcome = images_outcome(result, returncode)
        self._show_outcome(outcome)
        if outcome.ok and outcome.folder is not None:
            self._last_folder = outcome.folder
            self._open_folder_button.configure(state="normal")
        elif not outcome.ok:
            self._warn("解密图片", outcome.text)

    # ── opening results, menus, closing ───────────────────────────────────────

    def _open(self, path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            open_path(path)
        except OSError:
            self._warn("打不开", f"没能打开：\n{path}")

    def _open_html(self) -> None:
        self._open(self._last_html)

    def _open_folder(self) -> None:
        self._open(self._last_folder)

    def _choose_data_root(self, source: str) -> None:
        label = SOURCE_LABELS[source]
        chosen = filedialog.askdirectory(
            parent=self.root,
            title=f"选择{label}的数据文件夹",
            initialdir=self._data_roots.get(source) or str(Path.home()),
        )
        if not chosen:
            return
        self._data_roots[source] = str(Path(chosen))
        self.log(f"已指定{label}数据文件夹：{self._data_roots[source]}")
        self.probe()

    def _reset_data_roots(self) -> None:
        self._data_roots.clear()
        self.log("已恢复自动查找数据文件夹")
        self.probe()

    def _show_help(self) -> None:
        messagebox.showinfo("使用说明", HELP_TEXT, parent=self.root)

    def _show_about(self) -> None:
        messagebox.showinfo(
            "关于",
            f"chatlog-keeper {__version__}\n\n"
            "导出、备份你本人的微信 / QQ 聊天记录，仅限本地使用。",
            parent=self.root,
        )

    def _on_close(self) -> None:
        if self.busy and not self._ask(
            "正在运行", "有任务还在运行，退出会中止它。确定退出吗？", unattended=True
        ):
            return
        self.shutdown()

    def shutdown(self) -> None:
        if self._run is not None:
            self._run.cancel()
        self.root.destroy()


def _wrap_to_width(label: Any) -> None:
    """Re-wrap a stretched label whenever the window gives it a new width."""

    label.bind(
        "<Configure>",
        lambda event: label.configure(wraplength=max(event.width, 120)),
    )


def _derived_font(_root: Any, size_delta: int = 0, bold: bool = False) -> Any:
    from tkinter import font as tkfont

    derived = tkfont.nametofont("TkDefaultFont").copy()
    size = int(derived.cget("size"))
    # Tk stores point sizes as positive and pixel sizes as negative numbers.
    derived.configure(size=size + size_delta if size > 0 else size - size_delta)
    if bold:
        derived.configure(weight="bold")
    return derived


def _prepare_fonts(root: Any) -> None:
    """Use the system's Chinese UI font on Windows instead of glyph fallback."""

    if os.name != "nt":
        return
    from tkinter import font as tkfont

    if "Microsoft YaHei UI" not in tkfont.families(root):
        return
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=10)


def _enable_dpi_awareness() -> None:
    """Keep text sharp on scaled Windows displays; a no-op elsewhere."""

    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - older Windows or an already-set policy
        pass


def create_app(**options: Any) -> App:
    if tk is None:
        raise RuntimeError("this Python was built without Tk; the window cannot open")
    _enable_dpi_awareness()
    root = tk.Tk()
    _prepare_fonts(root)
    return App(root, **options)


def _selftest(arguments: Sequence[str]) -> int:
    """Open the real window, run one real ``probe`` through it, then close.

    Used by the build to prove that the frozen executable can create its window
    and start itself as the command-line child.  ``--report PATH`` writes what
    was observed as JSON, because a windowed executable has no console.
    """

    report_path = None
    if len(arguments) == 2 and arguments[0] == "--report":
        report_path = Path(arguments[1])
    report: Dict[str, Any] = {"ok": False, "version": __version__}
    code = 1
    try:
        app = create_app(interactive=False)
        root = app.root
        report["tk"] = root.tk.eval("info patchlevel")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and app.probe_count == 0:
            root.update()
            time.sleep(0.02)
        root.update()
        report["probe_finished"] = app.probe_count > 0
        report["states"] = {
            source: status.state for source, status in app.statuses.items()
        }
        report["window"] = [root.winfo_width(), root.winfo_height()]
        report["ok"] = (
            app.probe_count > 0
            and set(report["states"]) == set(SOURCES)
            and STATE_ERROR not in report["states"].values()
        )
        app.shutdown()
        code = 0 if report["ok"] else 1
    except Exception as exc:  # noqa: BLE001 - the report is the only output channel
        report["error"] = type(exc).__name__
        report["detail"] = str(exc)
    if report_path is not None:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--gui-selftest"]:
        return _selftest(arguments[1:])
    app = create_app()
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
