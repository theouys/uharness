#!/usr/bin/env python3
"""A tkinter desktop front-end for the U-HARNESS agent.

The window has two boxes: the prompt at the top and the agent's feedback
below. Everything the terminal version does is reachable, and every prompt
command also lives in the menu bar:

    File          Save conversation (Ctrl+S), open one (Ctrl+O), list them,
                  export what the pane is showing, quit
    Agent         auto-approve, dry run, thinking mode, token usage, verbosity,
                  free/paid model, show the model in use
    Conversation  forget the conversation, show the context size,
                  change the workspace
    Skills        list the skills available to this agent
    Tools         run the offline self-test
    View          clear the feedback pane
    Help          the command list, about this window

Typing a slash command straight into the prompt box works too, exactly as in
the terminal - and, as there, an unknown slash command never reaches the
model.

This file is a thin skin, not a fork: it imports uharness and drives the same
Harness object, so the tools, skills, saved sessions, autosave, context
compaction, approval modes and the dangerous-command blocklist are the
terminal version's. The one piece replaced is the terminal "allow?" prompt,
which becomes a modal dialog. Because the harness prints rather than returns
its progress, sys.stdout and sys.stderr are pointed at the feedback box for the
life of the window; that is why the pane reads like the terminal output it is.

Usage:
    python3 uharness-ui.py                            # empty window
    python3 uharness-ui.py "Summarise README.md"      # ask straight away
    python3 uharness-ui.py --workspace ~/project --yes
    python3 uharness-ui.py --selftest                 # offline tool checks

Every uharness.py option is accepted - run `python3 uharness-ui.py --help`.
Requires Python 3.10+, tkinter, a display, and the openai package.
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
from typing import Any

# import uharness.py from beside this file, whatever directory we were started in
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

_IMPORT_ERROR: str | None = None
try:
    import uharness as core
except ImportError as exc:          # tkinter missing on some slim installs
    core = None                      # type: ignore[assignment]
    _IMPORT_ERROR = f"uharness.py could not be imported:\n\n{exc}"
except SystemExit:                   # uharness prints its own "pip install openai"
    core = None                      # type: ignore[assignment]
    _IMPORT_ERROR = (
        "uharness.py needs the openai package:\n\n    pip install openai\n\n"
        "then start this window again."
    )


# --------------------------------------------------------------------------- #
# Colours and layout constants
# --------------------------------------------------------------------------- #

BG = "#15171b"          # window background
PANE_BG = "#1b1e24"     # feedback pane
INPUT_BG = "#22262e"    # prompt box
FG = "#d7dae0"          # default text
DIM = "#8b93a1"
GREEN = "#7ddc7d"
CYAN = "#66c9df"
YELLOW = "#e8c547"
RED = "#ff6b6b"
MAGENTA = "#c792ea"
BLUE = "#8ab4f8"
ANSWER = "#add8e6"      # light blue: the model's final answer, not commentary
SEP = "#4a4f57"

TAG_COLOURS: dict[str, dict[str, Any]] = {
    "body": {"foreground": FG},
    "dim": {"foreground": DIM},
    "you": {"foreground": BLUE, "font_bold": True},
    "assistant": {"foreground": GREEN, "font_bold": True},
    "answer": {"foreground": ANSWER},
    "tool": {"foreground": CYAN, "font_bold": True},
    "result": {"foreground": DIM},
    "warn": {"foreground": YELLOW},
    "error": {"foreground": RED, "font_bold": True},
    "ok": {"foreground": GREEN},
    "run": {"foreground": YELLOW, "font_bold": True},
    "skill": {"foreground": MAGENTA},
    "badge": {"foreground": CYAN, "font_bold": True},
    "add": {"foreground": GREEN},
    "rem": {"foreground": RED},
    "hunk": {"foreground": CYAN},
    "banner": {"foreground": CYAN, "font_bold": True},
    "sep": {"foreground": SEP},
}

# How a notice kind from uharness.notice() is coloured here.
_NOTICE_TAGS = {
    "warn": "warn",
    "error": "error",
    "deny": "error",
    "retry": "warn",
    "run": "run",
    "skill": "skill",
    "ok": "ok",
    "info": "dim",
    "context": "dim",
    "usage": "dim",
}


def classify_line(line: str) -> str:
    """Pick a colour tag for one line of harness output.

    The harness's renderers all start a status line with "  <glyph> <label>  ",
    so the glyph plus the first word is enough to tell an assistant heading, a
    tool call, an error, a diff line and so on apart without re-implementing
    any of the formatting. Anything unrecognised is plain body text.
    """
    stripped = line.strip()
    if not stripped:
        return "body"
    tokens = stripped.split(None, 1)
    glyph = tokens[0]
    rest = tokens[1] if len(tokens) > 1 else ""
    word = rest.split(None, 1)[0] if rest else ""

    if glyph in {"\u25b2", "!"}:                       # ▲ warn / ! warn
        return "warn" if word in {"warn", "retry"} else "body"
    if glyph in {"\u2716", "x"}:                       # ✖ error / x error
        return "error" if word in {"error", "denied"} else "body"
    if glyph in {"\u2714", "+"}:                       # ✔ ok
        return "ok"
    if glyph in {"\u25cf", "*"}:                       # ● assistant / ● free
        if word == "assistant":
            return "assistant"
        if word in {"free", "paid"}:
            return "badge"
        return "dim"
    if glyph == "\u2022":                              # • usage / • context / • info
        # Plumbing notices are dim in the terminal, label or no label. The
        # ASCII fallback glyph is "-", where only a label gives it away, so
        # that case is handled below and a bare "-" line stays body text.
        return "dim"
    if glyph == "-" and word in {"usage", "context"}:
        return "dim"
    if glyph in {"\u2699", "\u25b8", "\u21bb", ">", "~"}:
        # ⚙ tool call, ▸ run, ↻ retry (">" and "~" are their ASCII fallbacks)
        if word == "skill":
            return "skill"
        if word == "run":
            return "run"
        if word in {"warn", "retry"}:
            return "warn"
        if word in {"error", "denied"}:
            return "error"
        return "tool"
    if glyph in {"\u21b3", "->"}:                      # ↳ result
        return "result"
    if glyph == "\u276f":                              # ❯ you
        return "you"
    if glyph in {"\u2502", "|"}:                       # │ inside a block
        if rest.startswith("@@"):
            return "hunk"
        if rest.startswith(("+++", "---")):
            return "dim"
        if rest.startswith("+"):
            return "add"
        if rest.startswith("-"):
            return "rem"
        if rest.startswith("stderr"):
            return "warn"
        return "body"
    return "body"


# --------------------------------------------------------------------------- #
# Plumbing between the harness's prints and the window
# --------------------------------------------------------------------------- #

class PaneStream:
    """A file-like object that forwards every write into the feedback pane.

    The harness talks to its caller by printing, so replacing sys.stdout and
    sys.stderr with one of these is what puts the terminal transcript in the
    window. Writes can come from the worker thread and from the tool threads
    that pump command output, so they go through a queue that only the Tk
    thread ever drains.

    isatty() is False and encoding is utf-8: the first keeps ANSI colour out
    (nothing is echoing to a terminal any more), the second lets the box
    drawing glyphs through.
    """

    encoding = "utf-8"
    errors = "replace"
    closed = False

    def __init__(self, events: queue.Queue) -> None:
        self.events = events

    def write(self, text: str) -> int:
        if text:
            self.events.put(("text", str(text)))
        return len(text)

    def flush(self) -> None:
        return None

    def answer_start(self) -> None:
        """Called by the harness just before it prints the final answer.

        The pane cannot read the harness's colour codes (it strips them), so
        this marker is what tells it which block is the answer and should be
        drawn in light blue instead of the usual green commentary.
        """
        self.events.put(("answer",))

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:  # pragma: no cover - nothing should need a real fd
        raise OSError("the feedback pane is not a file descriptor")


# --------------------------------------------------------------------------- #
# Small modal dialogs
# --------------------------------------------------------------------------- #

class ApprovalDialog:
    """The "allow?" prompt, as a window: allow once, deny, or always allow."""

    def __init__(self, app: HarnessUI, box: dict[str, Any],
                 action: str, detail: str, dangerous: bool) -> None:
        self.app = app
        self.box = box

        win = tk.Toplevel(app.root)
        win.title("Approval needed")
        win.transient(app.root)
        win.configure(bg=BG)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", lambda: self._answer(False))

        head = tk.Label(
            win, text=f"The agent wants to {action.lower()}",
            bg=BG, fg=YELLOW, font=(app.family, app.size + 1, "bold"),
            anchor="w", justify="left",
        )
        head.pack(fill="x", padx=16, pady=(16, 4))

        body = tk.Label(
            win, text=detail, bg=BG, fg=FG, anchor="w", justify="left",
            wraplength=620, font=(app.family, app.size),
        )
        body.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        if dangerous:
            tk.Label(
                win,
                text=("warning: this command matches the dangerous-command "
                      "blocklist and was already refused"),
                bg=BG, fg=RED, anchor="w", justify="left", wraplength=620,
                font=(app.family, app.size - 1),
            ).pack(fill="x", padx=16, pady=(0, 12))

        buttons = tk.Frame(win, bg=BG)
        buttons.pack(fill="x", padx=16, pady=(0, 16))
        make_button(buttons, "Allow once", self._answer, True, primary=True)
        make_button(buttons, "Deny", self._answer, False)
        make_button(buttons, "Always allow", self._answer, True, always=True)
        for widget in buttons.winfo_children():
            widget.pack(side="right", padx=(8, 0))

        win.bind("<Return>", lambda _event: self._answer(True))
        win.bind("<Escape>", lambda _event: self._answer(False))
        win.update_idletasks()
        self._centre(win)
        win.grab_set()
        win.focus_set()

        self.win = win
        box["dialog"] = win

    def _centre(self, win: tk.Toplevel) -> None:
        root = self.app.root
        x = root.winfo_rootx() + max(0, (root.winfo_width() - win.winfo_width()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")

    def _answer(self, allow: bool, *, always: bool = False) -> None:
        self.app.resolve_approval(self.box, allow, always=always)


class SessionPicker:
    """Pick a saved conversation from a list instead of typing its number."""

    def __init__(self, app: HarnessUI, sessions: list[dict[str, Any]]) -> None:
        self.app = app
        self.sessions = sessions
        self.result: str | None = None

        win = tk.Toplevel(app.root)
        win.title("Open a saved conversation")
        win.transient(app.root)
        win.configure(bg=BG)
        win.geometry("760x420")
        win.protocol("WM_DELETE_WINDOW", self._cancel)

        tk.Label(
            win, text=f"{len(sessions)} saved conversation(s)",
            bg=BG, fg=CYAN, font=(app.family, app.size + 1, "bold"), anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 6))

        frame = tk.Frame(win, bg=BG)
        frame.pack(fill="both", expand=True, padx=14)
        self.listbox = tk.Listbox(
            frame, bg=INPUT_BG, fg=FG, selectbackground="#33507a",
            selectforeground="#ffffff", activestyle="none", relief="flat",
            font=(app.family, app.size), highlightthickness=0,
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        for item in sessions:
            self.listbox.insert(
                "end",
                f"{item['name']}    {item['messages']} messages    {item['saved_at']}",
            )
        self.listbox.selection_set(0)
        self.listbox.bind("<Double-Button-1>", lambda _event: self._open())
        self.listbox.bind("<<ListboxSelect>>", lambda _event: self._preview())

        self.preview = tk.Label(
            win, text="", bg=BG, fg=DIM, anchor="w", justify="left",
            wraplength=720, font=(app.family, app.size - 1),
        )
        self.preview.pack(fill="x", padx=14, pady=(8, 0))
        self._preview()

        buttons = tk.Frame(win, bg=BG)
        buttons.pack(fill="x", padx=14, pady=14)
        make_button(buttons, "Cancel", self._cancel)
        make_button(buttons, "Open", self._open, primary=True)
        for widget in buttons.winfo_children():
            widget.pack(side="right", padx=(8, 0))

        win.bind("<Return>", lambda _event: self._open())
        win.bind("<Escape>", lambda _event: self._cancel())
        win.focus_set()
        self.win = win
        win.wait_window(win)

    def _selected(self) -> dict[str, Any] | None:
        chosen = self.listbox.curselection()
        if not chosen:
            return None
        return self.sessions[chosen[0]]

    def _preview(self) -> None:
        item = self._selected()
        if item:
            self.preview.configure(text="what it starts with: " + item["preview"])

    def _open(self) -> None:
        item = self._selected()
        if item:
            self.result = item["name"]
        self.win.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.win.destroy()


def make_button(parent: tk.Misc, text: str, command: Any, *args: Any,
                primary: bool = False, **kwargs: Any) -> tk.Button:
    """A flat button that looks the same on every platform's ttk theme."""
    del kwargs
    return tk.Button(
        parent, text=text, command=lambda: command(*args),
        bg="#2f4f7f" if primary else INPUT_BG,
        fg="#ffffff" if primary else FG,
        activebackground="#3d6399" if primary else "#2b303a",
        activeforeground="#ffffff" if primary else FG,
        relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
        highlightthickness=0,
    )


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #

class HarnessUI:
    POLL_MS = 50            # how often the queue is drained into the pane
    MAX_PANE_LINES = 20_000
    TRIM_LINES = 2_000

    def __init__(self, root: tk.Tk, args: Any) -> None:
        self.root = root
        self.args = args
        self.console = sys.stdout      # a real console, for the odd exit note

        self.events: queue.Queue = queue.Queue()
        self.pending_text = ""         # a line the harness has not finished
        self.answering = False         # the next block is the final answer
        self.busy = False
        self.worker_ident: int | None = None
        self.approvals: list[dict[str, Any]] = []
        self.history: list[str] = []
        self.history_pos = 0
        self.closed = False

        # A tkinter callback that raises should land in the pane, not vanish.
        self.root.report_callback_exception = self._tk_error

        self._build_widgets()
        self._redirect_console()
        core.apply_verbosity(args.verbosity)
        self.harness = self.build_harness(args.workspace)
        self._build_menu()
        self._bind_keys()
        self.sync_vars()
        self.announce()

        self.root.after(self.POLL_MS, self._pump)
        self.prompt_text.focus_set()

        prompt = " ".join(args.prompt).strip()
        if prompt:
            self.root.after(200, lambda: self.submit(prompt))

    # -- construction ------------------------------------------------------ #

    def build_harness(self, workspace: str) -> core.Harness:
        """Create a Harness that asks for approval through a dialog."""
        path = Path(workspace).expanduser()
        if not path.is_dir():
            raise ValueError(f"workspace is not a directory: {path}")

        harness = core.Harness(
            path,
            model=self.args.model,
            auto_approve=self.args.yes,
            allow_dangerous=self.args.allow_dangerous,
            dry_run=self.args.dry_run,
            max_turns=max(1, self.args.max_turns),
            command_timeout=max(1, self.args.command_timeout),
            thinking=self.args.thinking,
            log_path=Path(self.args.log).expanduser() if self.args.log else None,
            show_usage=self.args.usage,
            autosave=not self.args.no_autosave,
        )
        self.install_approver(harness)
        return harness

    def install_approver(self, harness: core.Harness) -> None:
        """Swap the terminal "allow?" prompt for this window's dialog.

        Harness._approve is the single gate every write, edit, delete and
        command passes through, so replacing it on the instance keeps every
        rule around it - the dangerous-command blocklist, auto-approve, the
        dry run - exactly as the terminal version has them.
        """
        app = self

        def approve(action: str, detail: str, *, dangerous: bool = False) -> bool:
            if dangerous and not harness.allow_dangerous:
                return False
            if harness.auto_approve:
                return True
            return app.ask_approval(action, detail, dangerous=dangerous)

        harness._approve = approve  # type: ignore[method-assign]

    def _build_widgets(self) -> None:
        root = self.root
        root.configure(bg=BG)
        root.title("U-HARNESS UI")
        root.geometry("1140x840")
        root.minsize(760, 540)

        mono = tkfont.nametofont("TkFixedFont")
        self.family = mono.actual("family") or "Courier"
        self.size = 11

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover - platform dependent
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Header.TLabel", foreground=FG,
                        font=(self.family, self.size - 1))

        # --- header: which model, and whether a request is running ---------- #
        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        header.columnconfigure(1, weight=1)

        self.badge = ttk.Label(header, text="", style="Header.TLabel")
        self.badge.grid(row=0, column=0, sticky="w")
        self.state = ttk.Label(header, text="ready", style="Header.TLabel",
                               anchor="e")
        self.state.grid(row=0, column=1, sticky="e")

        # --- the prompt, at the top ---------------------------------------- #
        prompt_box = ttk.Frame(root)
        prompt_box.grid(row=1, column=0, sticky="ew", padx=12)
        prompt_box.columnconfigure(0, weight=1)

        ttk.Label(
            prompt_box,
            text=("Prompt   -   Enter sends, Shift+Enter adds a line, "
                  "Ctrl+\u2191/\u2193 walks your history, /command works too"),
            style="Header.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.prompt_text = tk.Text(
            prompt_box, height=6, wrap="word", undo=True,
            bg=INPUT_BG, fg=FG, insertbackground=FG,
            selectbackground="#33507a", selectforeground="#ffffff",
            relief="flat", bd=0, padx=10, pady=8,
            font=(self.family, self.size + 1), highlightthickness=1,
            highlightbackground="#2b303a", highlightcolor="#3d6399",
        )
        self.prompt_text.grid(row=1, column=0, sticky="ew")

        buttons = ttk.Frame(root)
        buttons.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 8))
        buttons.columnconfigure(0, weight=1)
        self.send_button = make_button(buttons, "Send", self.send, primary=True)
        self.send_button.grid(row=0, column=1, padx=(6, 0))
        self.stop_button = make_button(buttons, "Stop", self.stop)
        self.stop_button.configure(state="disabled")
        self.stop_button.grid(row=0, column=2, padx=(6, 0))
        clear = make_button(buttons, "Clear prompt", self.clear_prompt)
        clear.grid(row=0, column=3, padx=(6, 0))

        # --- the feedback, below ------------------------------------------- #
        feedback_box = ttk.Frame(root)
        feedback_box.grid(row=3, column=0, sticky="nsew", padx=12)
        feedback_box.columnconfigure(0, weight=1)
        feedback_box.rowconfigure(1, weight=1)
        root.rowconfigure(3, weight=1)
        root.columnconfigure(0, weight=1)

        ttk.Label(feedback_box, text="Feedback", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))

        self.feedback = tk.Text(
            feedback_box, wrap="word", state="disabled",
            bg=PANE_BG, fg=FG, relief="flat", bd=0, padx=10, pady=8,
            font=(self.family, self.size), highlightthickness=0,
            selectbackground="#33507a", selectforeground="#ffffff",
        )
        self.feedback.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(feedback_box, orient="vertical",
                               command=self.feedback.yview)
        self.feedback.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns")

        for name, spec in TAG_COLOURS.items():
            bold = spec.get("font_bold", False)
            self.feedback.tag_configure(
                name,
                foreground=spec["foreground"],
                font=(self.family, self.size, "bold") if bold
                else (self.family, self.size),
            )

        # --- footer --------------------------------------------------------- #
        self.footer = ttk.Label(root, text="", style="Header.TLabel")
        self.footer.grid(row=4, column=0, sticky="ew", padx=12, pady=(6, 8))

    def _build_menu(self) -> None:
        # The menu items below read and write these, so they exist first.
        self.var_approve = tk.BooleanVar(value=self.harness.auto_approve)
        self.var_dry = tk.BooleanVar(value=self.harness.dry_run)
        self.var_thinking = tk.BooleanVar(value=self.harness.thinking)
        self.var_usage = tk.BooleanVar(value=self.harness.show_usage)
        self.var_verbosity = tk.StringVar(value=self.harness.verbosity)
        self.var_tier = tk.StringVar(value="free" if self.harness.free else "paid")

        menu = tk.Menu(self.root)
        self.root.configure(menu=menu)

        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Undo last change", accelerator="Ctrl+Shift+Z",
                              command=lambda: self.command("/undo"))
        file_menu.add_command(label="Show undo history",
                              command=lambda: self.command("/undo list"))
        file_menu.add_separator()
        file_menu.add_command(label="Save conversation\u2026", accelerator="Ctrl+S",
                              command=lambda: self.command("/save"))
        file_menu.add_command(label="Open conversation\u2026", accelerator="Ctrl+O",
                              command=lambda: self.command("/open"))
        file_menu.add_command(label="List saved conversations",
                              command=lambda: self.command("/sessions"))
        file_menu.add_separator()
        file_menu.add_command(label="Export feedback\u2026", command=self.export_pane)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", accelerator="Ctrl+Q", command=self.on_close)
        menu.add_cascade(label="File", menu=file_menu)

        agent_menu = tk.Menu(menu, tearoff=False)
        agent_menu.add_checkbutton(label="Auto-approve tools", variable=self.var_approve,
                                   command=self.toggle_approve)
        agent_menu.add_checkbutton(label="Dry run (change nothing)",
                                   variable=self.var_dry, command=self.toggle_dry_run)
        agent_menu.add_checkbutton(label="Thinking / reasoning mode",
                                   variable=self.var_thinking,
                                   command=self.toggle_thinking)
        agent_menu.add_checkbutton(label="Show token usage", variable=self.var_usage,
                                   command=self.toggle_usage)
        agent_menu.add_separator()

        verbose_menu = tk.Menu(agent_menu, tearoff=False)
        for level in core.VERBOSITY_LEVELS:
            verbose_menu.add_radiobutton(
                label=level, value=level, variable=self.var_verbosity,
                command=lambda level=level: self.command(f"/verbose {level}"),
            )
        agent_menu.add_cascade(label="Verbosity", menu=verbose_menu)

        model_menu = tk.Menu(agent_menu, tearoff=False)
        model_menu.add_radiobutton(label="Free (Token Harbor)", value="free",
                                   variable=self.var_tier,
                                   command=lambda: self.command("/model free"))
        model_menu.add_radiobutton(label="Paid (DeepSeek)", value="paid",
                                   variable=self.var_tier,
                                   command=lambda: self.command("/model paid"))
        agent_menu.add_cascade(label="Model", menu=model_menu)
        agent_menu.add_command(label="Show current model",
                               command=lambda: self.command("/model"))
        menu.add_cascade(label="Agent", menu=agent_menu)

        conv_menu = tk.Menu(menu, tearoff=False)
        conv_menu.add_command(label="Forget this conversation",
                              command=lambda: self.command("/reset"))
        conv_menu.add_command(label="Show context size",
                              command=lambda: self.command("/context"))
        conv_menu.add_separator()
        conv_menu.add_command(label="Change workspace\u2026", command=self.change_workspace)
        menu.add_cascade(label="Conversation", menu=conv_menu)

        skills_menu = tk.Menu(menu, tearoff=False)
        skills_menu.add_command(label="List skills",
                                command=lambda: self.command("/skills"))
        skills_menu.add_command(label="Refresh skill index", command=self.refresh_skills)
        menu.add_cascade(label="Skills", menu=skills_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="Run offline self-test", command=self.run_selftest)
        menu.add_cascade(label="Tools", menu=tools_menu)

        view_menu = tk.Menu(menu, tearoff=False)
        view_menu.add_command(label="Clear feedback pane", accelerator="Ctrl+L",
                              command=self.clear_pane)
        menu.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Commands", command=lambda: self.command("/help"))
        help_menu.add_command(label="About this window", command=self.about)
        menu.add_cascade(label="Help", menu=help_menu)

        self.menu = menu        # menu state mirrors the harness via sync_vars()

    def _bind_keys(self) -> None:
        root = self.root
        self.prompt_text.bind("<Return>", self._on_return)
        self.prompt_text.bind("<Control-Return>",
                              lambda _event: (self.send(), "break")[1])
        self.prompt_text.bind("<Control-Up>", self.history_back)
        self.prompt_text.bind("<Control-Down>", self.history_forward)
        root.bind("<Control-s>", lambda _event: (self.command("/save"), "break")[1])
        # Shift+Z, not plain Ctrl+Z: the prompt box keeps its own text undo.
        root.bind("<Control-Shift-z>", lambda _event: (self.command("/undo"), "break")[1])
        root.bind("<Control-Shift-Z>", lambda _event: (self.command("/undo"), "break")[1])
        root.bind("<Control-o>", lambda _event: (self.command("/open"), "break")[1])
        root.bind("<Control-l>", lambda _event: (self.clear_pane(), "break")[1])
        root.bind("<Control-q>", lambda _event: (self.on_close(), "break")[1])

    # -- console redirection ---------------------------------------------- #

    def _redirect_console(self) -> None:
        """Point the harness's prints at the pane (see PaneStream)."""
        stream = PaneStream(self.events)
        sys.stdout = stream            # type: ignore[assignment]
        sys.stderr = stream            # type: ignore[assignment]
        self.stream = stream

    def _restore_console(self) -> None:
        sys.stdout = self.console
        sys.stderr = sys.__stderr__ or self.console

    # -- writing into the pane -------------------------------------------- #

    def _at_bottom(self) -> bool:
        try:
            return self.feedback.yview()[1] > 0.995
        except tk.TclError:  # pragma: no cover - widget already gone
            return True

    def _insert(self, pairs: list[tuple[str | None, str]]) -> None:
        """Append (tag, text) runs to the pane, keeping it scrolled to the end."""
        pairs = [(tag, text) for tag, text in pairs if text]
        if not pairs:
            return
        follow = self._at_bottom()
        widget = self.feedback
        try:
            widget.configure(state="normal")
            for tag, text in pairs:
                # Belt and braces: colour must never reach the widget even if
                # the environment asks the harness for it.
                widget.insert("end", core.strip_ansi(text),
                              (tag,) if tag else ())
            widget.configure(state="disabled")
            if follow:
                widget.see("end")
            self._trim_pane()
        except tk.TclError:  # pragma: no cover - closing down
            pass

    def _trim_pane(self) -> None:
        """Keep the pane bounded so a very long session stays responsive."""
        try:
            lines = int(self.feedback.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):  # pragma: no cover
            return
        if lines <= self.MAX_PANE_LINES:
            return
        try:
            self.feedback.configure(state="normal")
            self.feedback.delete("1.0", f"{self.TRIM_LINES}.0")
            self.feedback.configure(state="disabled")
        except tk.TclError:  # pragma: no cover
            pass

    def _take_pending(self) -> str:
        text, self.pending_text = self.pending_text, ""
        return text

    def write(self, text: str, tag: str | None = None) -> None:
        """Write straight into the pane from the Tk thread."""
        if not text:
            return
        self._insert([(tag, self._take_pending() + text)])

    def notice(self, kind: str, text: str) -> None:
        """A status line in the same shape the harness uses for its own."""
        line = core.notice(kind, text, force=True)
        self.write((line or "") + "\n", _NOTICE_TAGS.get(kind, "dim"))

    def separator(self, label: str = "") -> None:
        if label:
            rule = "\u2500" * max(0, 58 - len(label))
            self.write(f"\n\u2500\u2500 {label} {rule}\n", "sep")
        else:
            self.write("\n", "sep")

    def _flush_text(self) -> None:
        """Turn queued console output into tagged lines."""
        data = self.pending_text
        if not data:
            return
        lines = data.split("\n")
        self.pending_text = lines.pop()      # last piece has no newline yet
        if not lines:
            return
        pairs: list[tuple[str | None, str]] = []
        for line in lines:
            tag = self._answer_tag(line, classify_line(line))
            text = line + "\n"
            if pairs and pairs[-1][0] == tag:
                pairs[-1] = (tag, pairs[-1][1] + text)
            else:
                pairs.append((tag, text))
        self._insert(pairs)

    def _answer_tag(self, line: str, tag: str) -> str:
        """Keep the final answer's heading and rail rows in the answer colour.

        The flag is set by the harness's `answer_start()` marker and cleared
        by the first line that cannot belong to the block, so a later tool
        call or notice goes back to its normal colour.
        """
        if not self.answering:
            return tag
        stripped = line.strip()
        if not stripped or tag == "assistant" or stripped[:1] in {"\u2502", "|"}:
            return "answer"
        self.answering = False
        return tag

    # -- the queue pump ----------------------------------------------------- #

    def _pump(self) -> None:
        if self.closed:
            return
        try:
            while True:
                try:
                    item = self.events.get_nowait()
                except queue.Empty:
                    break
                kind = item[0]
                if kind == "text":
                    self.pending_text += item[1]
                elif kind == "answer":
                    # Flush what came before so it keeps its own colour, then
                    # mark the block that starts on the next line.
                    if self.pending_text:
                        self.pending_text += "\n"
                    self._flush_text()
                    self.answering = True
                elif kind == "approval":
                    _, box, action, detail, dangerous = item
                    self.show_approval(box, action, detail, dangerous)
                elif kind == "done":
                    self.finish_busy(item[1] if len(item) > 1 else None)
            self._flush_text()
        except Exception:  # never let the pump die and freeze the window
            self.write("\n  \u2716 error  the window's queue pump failed\n"
                       + traceback.format_exc() + "\n", "error")
        if not self.closed:
            self.root.after(self.POLL_MS, self._pump)

    def _tk_error(self, exc_type: Any, exc: Any, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        self.write("\n  \u2716 error  a window callback failed\n" + text, "error")

    # -- approval ----------------------------------------------------------- #

    def ask_approval(self, action: str, detail: str, *, dangerous: bool = False) -> bool:
        """Block the worker thread until the dialog in the Tk thread answers."""
        event = threading.Event()
        box: dict[str, Any] = {"event": event, "answer": False, "dialog": None}
        self.events.put(("approval", box, action, detail, dangerous))
        event.wait()
        if box["answer"]:
            print(core.notice("ok", f"allowed: {action.lower()} - {detail}", force=True))
        else:
            print(core.notice("deny", f"denied: {action.lower()} - {detail}",
                              stream=sys.stderr, force=True), file=sys.stderr)
        return bool(box["answer"])

    def show_approval(self, box: dict[str, Any], action: str, detail: str,
                      dangerous: bool) -> None:
        """Tk side of ask_approval: put the dialog up and let the pump continue."""
        if self.closed:
            self.resolve_approval(box, False)
            return
        self.approvals.append(box)
        ApprovalDialog(self, box, action, detail, dangerous)

    def resolve_approval(self, box: dict[str, Any], allow: bool, *,
                         always: bool = False) -> None:
        """Answer a pending approval exactly once, then release the worker."""
        if box["event"].is_set():
            return
        if always:
            self.harness.set_auto_approve(True)
        box["answer"] = bool(allow)
        dialog = box.get("dialog")
        if dialog is not None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:  # pragma: no cover
                pass
        if box in self.approvals:
            self.approvals.remove(box)
        box["event"].set()
        self.sync_vars()

    # -- running a request -------------------------------------------------- #

    def start_work(self, work: Any, label: str) -> None:
        """Run `work` on a worker thread so the window stays alive."""
        if self.busy:
            self.notice("warn", "a request is already running - wait or press Stop")
            return
        self.busy = True
        # A fresh request starts in commentary colour, whatever the last turn
        # left behind (an interrupted answer never reached finish_busy).
        self.answering = False
        self.state.configure(text=label)
        self.send_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        def target() -> None:
            self.worker_ident = threading.get_ident()
            error: BaseException | None = None
            try:
                work()
            except BaseException as exc:      # noqa: BLE001 - reported, never lost
                error = exc
            finally:
                self.worker_ident = None
                self.events.put(("done", error))

        threading.Thread(target=target, name="uharness-worker", daemon=True).start()

    def finish_busy(self, error: BaseException | None) -> None:
        self.busy = False
        self.worker_ident = None
        self._flush_text()
        if self.pending_text:
            self.write(self._take_pending() + "\n")
        self.answering = False
        if error is not None:
            if isinstance(error, KeyboardInterrupt):
                self.notice("warn", "interrupted - back at the prompt")
            else:
                self.write(f"  \u2716 error  {type(error).__name__}: {error}\n",
                           "error")
        self.send_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.state.configure(text="ready")
        self.sync_vars()
        self.prompt_text.focus_set()

    def stop(self) -> None:
        """Ask the running request to stop, the way Ctrl+C does in a terminal.

        The interrupt is raised in the worker thread only, where the harness
        already handles it: the API call, the tool loop and the command runner
        all clean up after a KeyboardInterrupt. While the model is thinking the
        interrupt lands when the request returns.
        """
        ident = self.worker_ident
        if not self.busy or ident is None:
            return
        try:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(ident), ctypes.py_object(KeyboardInterrupt)
            )
        except Exception:  # pragma: no cover - only fails off-CPython
            self.notice("warn", "this Python cannot interrupt a thread - close the "
                                "window to stop the request")
            return
        self.state.configure(text="stopping\u2026")
        self.stop_button.configure(state="disabled")

    # -- the prompt --------------------------------------------------------- #

    def _on_return(self, event: tk.Event) -> str | None:
        if event.state & 0x0001:      # Shift held: let the newline through
            return None
        self.send()
        return "break"

    def send(self) -> None:
        text = self.prompt_text.get("1.0", "end-1c").strip()
        if not text:
            return
        if self.busy:
            self.notice("warn", "a request is running - wait or press Stop")
            return
        self.prompt_text.delete("1.0", "end")
        self.history.append(text)
        self.history_pos = len(self.history)
        self.echo(text)

        if text.startswith("/"):
            self.command(text)
            return
        harness = self.harness
        self.start_work(lambda: harness.run(text), "thinking\u2026")

    def submit(self, text: str) -> None:
        """Put `text` in the prompt box and send it (used for a CLI prompt)."""
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", text)
        self.send()

    def echo(self, text: str) -> None:
        self.write("\n")
        for index, line in enumerate(text.splitlines() or [""]):
            prefix = "\u276f " if index == 0 else "  "
            self.write(prefix + line + "\n", "you")

    def clear_prompt(self) -> None:
        self.prompt_text.delete("1.0", "end")

    def history_back(self, _event: tk.Event | None = None) -> str:
        if not self.history:
            return "break"
        self.history_pos = max(0, self.history_pos - 1)
        self._set_prompt(self.history[self.history_pos])
        return "break"

    def history_forward(self, _event: tk.Event | None = None) -> str:
        if not self.history:
            return "break"
        if self.history_pos >= len(self.history) - 1:
            self.history_pos = len(self.history)
            self._set_prompt("")
        else:
            self.history_pos += 1
            self._set_prompt(self.history[self.history_pos])
        return "break"

    def _set_prompt(self, text: str) -> None:
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", text)
        self.prompt_text.mark_set("insert", "end")
        self.prompt_text.see("end")

    # -- the prompt commands ------------------------------------------------ #
    #
    # Same words, same meaning as the terminal's slash commands, so a habit
    # from one interface works in the other. The menu items call this same
    # dispatcher, which is why the two can never drift apart.

    def command(self, line: str) -> None:
        word, _, argument = line.partition(" ")
        word = word.lower()
        argument = argument.strip()

        if word in {"/exit", "/quit"}:
            self.on_close()
        elif word in {"/help", "/?"}:
            core.show_commands()
        elif word == "/save":
            self.save_conversation(argument)
        elif word == "/open":
            self.open_conversation(argument)
        elif word in {"/sessions", "/ls"}:
            core.show_sessions(self.harness)
        elif word in {"/skills", "/skill"}:
            core.show_skills(self.harness)
        elif word in {"/context", "/ctx"}:
            core.show_context(self.harness)
        elif word == "/model":
            if argument in {"free", "paid"}:
                self.set_model(argument == "free")
            elif argument:
                self.notice("warn", "usage: /model [free|paid]")
            else:
                self.show_model()
        elif word in {"/approve", "/auto"}:
            if argument in {"", "toggle"}:
                self.set_approve(not self.harness.auto_approve)
            elif argument in {"on", "off"}:
                self.set_approve(argument == "on")
            else:
                self.notice("warn", "usage: /approve [on|off] - blank toggles")
        elif word in {"/verbose", "/verbosity"}:
            self.set_verbosity(argument or self.harness.verbosity)
        elif word == "/reset":
            self.reset_conversation()
        elif word == "/undo":
            self.undo_last(argument)
        else:
            self.notice("warn", f"unknown command {word} - Help > Commands lists them")

    def save_conversation(self, name: str = "") -> None:
        if not name:
            name = simpledialog.askstring(
                "Save conversation",
                "Name for this conversation (blank for a timestamp):",
                parent=self.root,
            )
            if name is None:
                return
            name = name.strip()
        try:
            path = self.harness.save_session(name or None)
        except (OSError, ValueError) as exc:
            self.notice("error", f"could not save: {exc}")
            return
        self.notice("ok", f"saved {self.harness.rel(path)}")

    def open_conversation(self, selector: str = "") -> None:
        if self.busy:
            self.notice("warn", "a request is running - wait before opening a "
                                "conversation")
            return
        if not selector:
            sessions = self.harness.list_sessions()
            if not sessions:
                self.notice(
                    "info",
                    "no saved conversations yet - File > Save writes one to "
                    f"{self.harness.rel(self.harness.sessions_dir)}/",
                )
                return
            selector = SessionPicker(self, sessions).result or ""
            if not selector:
                return
        try:
            item = self.harness.load_session(selector)
        except ValueError as exc:
            self.notice("error", str(exc))
            return
        self.notice("ok", f"opened {item['name']} - {item['restored']} messages, "
                          f"saved {item['saved_at']}")
        self.render_history()
        self.sync_vars()

    def render_history(self) -> None:
        """Show the conversation that was just opened, the way a chat would.

        Tool results are summarised rather than replayed: they are the bulk of
        a saved session and the model still has every one of them.
        """
        messages = [
            message for message in self.harness.messages
            if message.get("role") != "system"
        ]
        if not messages:
            self.notice("info", "this conversation is empty")
            return
        tools = sum(1 for message in messages if message.get("role") == "tool")
        self.separator(f"{len(messages)} messages, {tools} tool result(s) not "
                       "replayed")
        for message in messages:
            role = message.get("role")
            content = str(message.get("content") or "")
            if role == "user":
                if content.startswith(core.SKILL_BLOCK_PREFIX):
                    first = content.splitlines()[2] if len(
                        content.splitlines()) > 2 else content
                    self.write(f"  [skills] {core.short(first, 90)}\n", "skill")
                    continue
                self.write("\n")
                for index, line in enumerate(core.clip(content, 4_000,
                                                       keep="head").splitlines()):
                    prefix = "\u276f " if index == 0 else "  "
                    self.write(prefix + line + "\n", "you")
            elif role == "assistant" and content.strip():
                # A message with no tool calls after it was the answer to its
                # request, so it is replayed in the answer colour.
                final = not message.get("tool_calls")
                self.write("\n" + core.assistant_block(
                    core.clip(content, 4_000, keep="head"),
                    final=final) + "\n", "answer" if final else "body")
        self.separator()

    def reset_conversation(self) -> None:
        if self.busy:
            self.notice("warn", "a request is running - wait before clearing it")
            return
        self.harness.reset()
        self.separator("conversation cleared")

    def undo_last(self, argument: str = "") -> None:
        """Revert the last request's file changes (or list what is recorded).

        The wording, the ordering and the safety rules live in uharness.show_undo
        and are reused here, so the window and the terminal cannot disagree.
        """
        if self.busy:
            self.notice("warn", "a request is running - stop it before undoing")
            return
        core.show_undo(self.harness, argument)

    def set_model(self, free: bool) -> None:
        try:
            self.harness.set_free(free)
        except ValueError as exc:
            self.notice("error", f"cannot switch: {exc}")
        self.sync_vars()

    def show_model(self) -> None:
        self.write("\n" + core.tier_badge(self.harness) + "\n", "badge")

    def set_approve(self, enabled: bool) -> None:
        self.harness.set_auto_approve(enabled)
        self.sync_vars()

    def toggle_approve(self) -> None:
        self.set_approve(bool(self.var_approve.get()))

    def set_verbosity(self, level: str) -> None:
        try:
            self.harness.set_verbosity(level)
        except ValueError as exc:
            self.notice("warn", str(exc))
        self.sync_vars()

    def toggle_dry_run(self) -> None:
        self.harness.dry_run = bool(self.var_dry.get())
        self.notice("info", "dry run on - nothing will be changed"
                    if self.harness.dry_run else "dry run off - changes are real")
        self.sync_vars()

    def toggle_thinking(self) -> None:
        self.harness.thinking = bool(self.var_thinking.get())
        self.notice("info", "thinking mode on"
                    if self.harness.thinking else "thinking mode off")
        self.sync_vars()

    def toggle_usage(self) -> None:
        self.harness.show_usage = bool(self.var_usage.get())
        self.notice("info", "token usage after each request"
                    if self.harness.show_usage else "token usage hidden")
        self.sync_vars()

    # -- menu odds and ends -------------------------------------------------- #

    def clear_pane(self) -> None:
        self.pending_text = ""
        self.answering = False
        try:
            self.feedback.configure(state="normal")
            self.feedback.delete("1.0", "end")
            self.feedback.configure(state="disabled")
        except tk.TclError:  # pragma: no cover
            pass

    def export_pane(self) -> None:
        text = self.feedback.get("1.0", "end-1c")
        if not text.strip():
            self.notice("info", "the feedback pane is empty")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export the feedback pane",
            defaultextension=".txt", initialfile="uharness-transcript.txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            self.notice("error", f"could not write {path}: {exc}")
            return
        self.notice("ok", f"wrote {path}")

    def refresh_skills(self) -> None:
        self.harness.refresh_system_prompt()
        core.show_skills(self.harness)

    def run_selftest(self) -> None:
        if self.busy:
            self.notice("warn", "a request is running - try the self-test after it")
            return
        self.separator("offline self-test")

        def work() -> None:
            code = core.run_selftest()
            print(core.notice("ok" if code == 0 else "error",
                              f"self-test finished with exit {code}", force=True))

        self.start_work(work, "self-test\u2026")

    def change_workspace(self) -> None:
        if self.busy:
            self.notice("warn", "a request is running - wait before changing the "
                                "workspace")
            return
        if len(self.harness.messages) > 1 and not messagebox.askyesno(
            "Change workspace",
            "Changing the workspace starts a new conversation.\n\n"
            "The current one is already autosaved, so /open can bring it back. "
            "Continue?",
            parent=self.root,
        ):
            return
        directory = filedialog.askdirectory(
            parent=self.root, title="Workspace for the agent",
            initialdir=str(self.harness.workspace), mustexist=True,
        )
        if not directory:
            return
        try:
            harness = self.build_harness(directory)
        except (OSError, ValueError) as exc:
            self.notice("error", f"could not use that directory: {exc}")
            return
        self.harness = harness
        self.clear_pane()
        self.separator(f"workspace {harness.workspace}")
        self.sync_vars()
        self.notice("info", "new conversation in the new workspace")

    def about(self) -> None:
        harness = self.harness
        messagebox.showinfo(
            "About U-HARNESS UI",
            "\n".join([
                "U-HARNESS UI - the agent harness, with a window.",
                "",
                f"model:      {harness.model} ({'free' if harness.free else 'paid'})",
                f"workspace:  {harness.workspace}",
                f"sessions:   {harness.rel(harness.sessions_dir)}",
                f"skills:     {harness.skills.describe_roots()}",
                f"tools:      {len(core.TOOLS)}",
                f"autosave:   {'on' if harness.autosave else 'off'}",
                "",
                f"uharness.py {Path(core.__file__).resolve()}",
                f"Python {sys.version.split()[0]}   Tk {tk.TkVersion}",
            ]),
            parent=self.root,
        )

    # -- the badge, footer and menus ----------------------------------------- #

    def sync_vars(self) -> None:
        harness = self.harness
        self.var_approve.set(harness.auto_approve)
        self.var_dry.set(harness.dry_run)
        self.var_thinking.set(harness.thinking)
        self.var_usage.set(harness.show_usage)
        self.var_verbosity.set(harness.verbosity)
        self.var_tier.set("free" if harness.free else "paid")

        tier = "free" if harness.free else "paid"
        self.badge.configure(
            text=f"\u25cf {tier}  {harness.model}",
            foreground=GREEN if harness.free else YELLOW,
        )
        approval = "auto-approve" if harness.auto_approve else "ask first"
        extra = " \u00b7 dry run" if harness.dry_run else ""
        extra += " \u00b7 thinking" if harness.thinking else ""
        self.footer.configure(
            text=(f"{harness.workspace}   |   {harness.verbosity} \u00b7 {approval}"
                  f"{extra}   |   "
                  f"Enter sends, Shift+Enter newline, Ctrl+S save, Ctrl+O open")
        )

    def announce(self) -> None:
        """The startup panel, into the pane instead of the terminal."""
        harness = self.harness
        self.write(core.banner() + "\n", "banner")

        rows: list[tuple[str, str]] = [
            ("model", harness.model),
            ("profile", "free" if harness.free else "paid"),
            ("workspace", str(harness.workspace)),
            ("tools", f"{len(core.TOOLS)} available"),
        ]
        if harness.dry_run:
            rows.append(("mode", "dry run (nothing will be changed)"))
        else:
            rows.append((
                "approval",
                "auto (--yes)" if harness.auto_approve
                else "ask before write/edit/delete/execute",
            ))
        if harness.autosave:
            rows.append(("autosave", f"after every turn to "
                                     f"{core.SESSION_DIR_NAME}/"))
        self.write(core.panel("session", rows) + "\n")

        self.write("\n")
        self.notice("info", "every /command is in the menu bar - Help > Commands "
                            "prints the list")
        self.notice("info", "type a prompt and press Enter; @{skill} works here too")

        if not (os.environ.get(core.FREE_API_KEY_ENV)
                or os.environ.get(core.PAID_API_KEY_ENV)):
            self.notice("error", "no API key is set - export one before sending a "
                                 "prompt:")
            self.write(
                f'    export {core.FREE_API_KEY_ENV}="thk_live_..."'
                f"   (free tier)\n"
                f'    export {core.PAID_API_KEY_ENV}="sk-..."'
                f"          (paid tier)\n",
                "dim",
            )

    # -- closing ------------------------------------------------------------ #

    def autosave_on_exit(self) -> None:
        harness = getattr(self, "harness", None)
        if harness is None or not harness.autosave:
            return
        try:
            path = harness.autosave_session()
        except (OSError, ValueError) as exc:
            print(f"  ! warn  could not autosave: {exc}", file=self.console)
            return
        if path is not None:
            print(
                core.notice(
                    "info",
                    f"autosaved {harness.rel(path)} - /open {path.stem} carries "
                    "on from here",
                    stream=self.console,
                ),
                file=self.console,
            )

    def on_close(self) -> None:
        if self.closed:
            return
        if self.busy:
            if not messagebox.askyesno(
                "Quit",
                "A request is still running. Stop it and quit?",
                parent=self.root,
            ):
                return
            for box in list(self.approvals):
                self.resolve_approval(box, False)
            self.stop()
        self.autosave_on_exit()
        self.closed = True
        self._restore_console()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def fallback_parser() -> Any:
    """A parser for when uharness.py cannot be imported.

    The real options come from uharness.build_parser(), but that needs the
    agent. This mirrors the subset a user might type before installing
    anything, so `--help` explains the situation instead of a traceback.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="uharness-ui",
        description=(
            "The U-HARNESS agent in a tkinter window (prompt on top, feedback "
            "below).\nuharness.py could not be imported, so only this reduced "
            "option set is available."
        ),
    )
    parser.add_argument("prompt", nargs="*", help="what you want the agent to do")
    parser.add_argument("-w", "--workspace", default=os.getcwd(),
                        help="directory the agent may work in")
    parser.add_argument("--selftest", action="store_true",
                        help="offline checks of the tool layer")
    return parser


def build_parser() -> Any:
    """The terminal parser, so both front-ends take the same flags."""
    if core is None:
        return fallback_parser()

    parser = core.build_parser()
    parser.prog = "uharness-ui"
    parser.description = (
        "The U-HARNESS agent in a tkinter window: prompt on top, feedback "
        "below, every /command in the menu bar.\n"
        "Takes the same options as uharness.py."
    )
    parser.epilog = (
        "Examples:\n"
        "  python3 uharness-ui.py\n"
        '  python3 uharness-ui.py "Summarise README.md"\n'
        "  python3 uharness-ui.py --workspace ~/project --yes\n"
        "  python3 uharness-ui.py --verbosity full --usage\n"
        "  python3 uharness-ui.py --selftest     # offline, no window\n"
        "\n"
        "In the window: Enter sends, Shift+Enter adds a line, Ctrl+S saves the\n"
        "conversation, Ctrl+O opens one, Ctrl+L clears the pane, Ctrl+Q quits.\n"
        "Slash commands typed into the prompt box work exactly as they do in\n"
        "the terminal, and every one of them is also in the menu bar.\n"
    )
    return parser


def report_fatal(title: str, message: str) -> None:
    """Report a start-up failure, without assuming someone can click OK.

    A modal box is right when the window was launched from the application
    grid and wrong everywhere else - piped, over SSH, in a test - where it
    would hang for ever waiting for a click that never comes. A display
    decides which; the message always reaches stderr as well, which is the
    launcher's log file when there is no terminal.
    """
    print(f"{title}: {message}", file=sys.__stderr__ or sys.stderr)
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        root = tk.Tk(className="uharness-ui")
        root.withdraw()
        messagebox.showerror(title, message, parent=root)
        root.destroy()
    except tk.TclError:  # pragma: no cover - display went away mid-call
        pass


def main() -> int:
    # Argument parsing comes first: --help and a bad flag must both work even
    # when the agent cannot be imported, so build_parser() has a fallback.
    args = build_parser().parse_args()

    if core is None:
        report_fatal("U-HARNESS UI", _IMPORT_ERROR or "uharness.py is missing")
        return 2

    if args.selftest:
        return core.run_selftest()

    workspace = Path(args.workspace).expanduser()
    if not workspace.is_dir():
        report_fatal("U-HARNESS UI", f"workspace is not a directory:\n{workspace}")
        return 2
    args.workspace = str(workspace)

    # className sets WM_CLASS, which is what a desktop shell matches against
    # StartupWMClass in the .desktop file - without it the window shows the
    # generic Python icon in the dock instead of U-HARNESS's own.
    root = tk.Tk(className="uharness-ui")
    try:
        app = HarnessUI(root, args)
    except Exception as exc:            # pragma: no cover - startup failures
        root.destroy()
        report_fatal("U-HARNESS UI",
                     f"the window could not start:\n\n{type(exc).__name__}: {exc}")
        return 2

    root.protocol("WM_DELETE_WINDOW", app.on_close)
    try:
        root.mainloop()
    finally:
        app._restore_console()
    return 0


if __name__ == "__main__":
    sys.exit(main())
