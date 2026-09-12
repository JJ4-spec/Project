#!/usr/bin/env python3
"""
Log Watcher — desktop GUI for log_watcher_agent.py

A dark-themed control panel for the log monitor: start/stop watching,
watch raw entries and model explanations stream in live, browse past
alerts, and export them. Pure standard library (tkinter) — no extra
installs beyond what log_watcher_agent.py already needs.

Run:
    python3 log_watcher_gui.py

Requires log_watcher_agent.py to be in the same folder.
"""

from __future__ import annotations

import json
import platform
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import log_watcher_agent as agent


# ---------------------------------------------------------------------------
# Palette / fonts — consistent, deliberate dark theme (not default ttk look)
# ---------------------------------------------------------------------------
COLOR_BG = "#0F1226"
COLOR_SURFACE = "#171B36"
COLOR_SURFACE_2 = "#1E2344"
COLOR_BORDER = "#2A2F55"
COLOR_TEXT = "#E7E6F5"
COLOR_TEXT_MUTED = "#9797B8"
COLOR_ACCENT = "#F2B84B"       # amber — primary actions, watching state
COLOR_ACCENT_2 = "#6C7BFF"     # violet — informational accents
COLOR_OK = "#6FCF97"           # idle/good
COLOR_WARN = "#F2B84B"
COLOR_ERROR = "#F26B6B"
COLOR_CRITICAL = "#FF4D4D"

FONT_FAMILY_UI = {
    "Windows": "Segoe UI",
    "Darwin": "Helvetica Neue",
}.get(platform.system(), "DejaVu Sans")

FONT_FAMILY_MONO = {
    "Windows": "Consolas",
    "Darwin": "Menlo",
}.get(platform.system(), "DejaVu Sans Mono")


# ---------------------------------------------------------------------------
# Source construction that never calls sys.exit — safe to run on a GUI thread
# ---------------------------------------------------------------------------
def create_source(mode: str, file_path: str, interval: int):
    """mode is 'auto' or 'file'. Raises RuntimeError on failure instead of
    exiting the process, unlike agent.build_source()."""
    if mode == "file":
        if not file_path:
            raise RuntimeError("Choose a log file to watch, or switch to Auto-detect.")
        return agent.FileTailSource(file_path)

    system = platform.system()
    if system == "Linux":
        return agent.JournalctlSource()
    if system == "Darwin":
        return agent.MacLogSource(lookback_seconds=interval + 5)
    raise RuntimeError(
        "No built-in system log source on this platform (Windows). "
        "Switch to 'Specific file' and pick a log to watch instead."
    )


# ---------------------------------------------------------------------------
# Background worker — runs the poll loop, reports everything via a queue
# ---------------------------------------------------------------------------
class WatcherThread(threading.Thread):
    def __init__(self, config: dict, out_queue: "queue.Queue", stop_event: threading.Event):
        super().__init__(daemon=True)
        self.config = config
        self.out_queue = out_queue
        self.stop_event = stop_event

    def emit(self, kind: str, **payload):
        self.out_queue.put({"kind": kind, "time": time.strftime("%H:%M:%S"), **payload})

    def run(self):
        try:
            source = create_source(self.config["mode"], self.config["file_path"], self.config["interval"])
        except RuntimeError as e:
            self.emit("source_error", message=str(e))
            return

        agent.MODEL = self.config["model"] or agent.MODEL
        deduper = agent.Deduper(agent.DEDUP_WINDOW_SECONDS)
        consecutive_failures = 0
        cycle = 0

        self.emit("started", mode=self.config["mode"], model=agent.MODEL)

        while not self.stop_event.is_set():
            cycle += 1
            interval = self.config["interval"]

            try:
                raw_entries = source.poll()
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                self.emit("source_failure", message=str(e), count=consecutive_failures)
                if consecutive_failures == agent.FAILURE_ALERT_THRESHOLD:
                    self.emit("monitor_failure", message=str(e), count=consecutive_failures)
                self.emit("cycle", n=cycle, new_entries=0)
                if self.stop_event.wait(interval):
                    break
                continue

            new_entries = [e for e in raw_entries if deduper.is_new(e["message"])]
            deduper.cleanup()

            if new_entries:
                self.emit("entries", entries=new_entries)

            if not new_entries:
                self.emit("cycle", n=cycle, new_entries=0)
            else:
                self.emit("analyzing", n=len(new_entries))
                try:
                    agent.MODEL = self.config["model"] or agent.MODEL
                    result = agent.explain_entries(new_entries)
                    consecutive_failures = 0
                    if result.get("alert"):
                        self.emit("alert", result=result, entries=new_entries)
                    else:
                        self.emit("routine", summary=result.get("summary", ""))
                except Exception as e:
                    consecutive_failures += 1
                    self.emit("explain_failure", message=str(e), count=consecutive_failures)
                    if consecutive_failures == agent.FAILURE_ALERT_THRESHOLD:
                        self.emit("monitor_failure", message=str(e), count=consecutive_failures)
                self.emit("cycle", n=cycle, new_entries=len(new_entries))

            if self.stop_event.wait(interval):
                break

        self.emit("stopped")


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------
class LogWatcherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Log Watcher")
        self.geometry("980x680")
        self.minsize(820, 560)
        self.configure(bg=COLOR_BG)

        self.out_queue: "queue.Queue" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: WatcherThread | None = None
        self.is_watching = False
        self.alert_records = []   # list of (result, entries) for export/detail lookup

        self._build_style()
        self._build_menu()
        self._build_layout()
        self._poll_queue()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- styling -----------------------------------------------------------
    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        base_font = (FONT_FAMILY_UI, 10)
        bold_font = (FONT_FAMILY_UI, 10, "bold")

        style.configure(".", background=COLOR_BG, foreground=COLOR_TEXT, font=base_font)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("Surface.TFrame", background=COLOR_SURFACE)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=base_font)
        style.configure("Muted.TLabel", background=COLOR_BG, foreground=COLOR_TEXT_MUTED, font=base_font)
        style.configure("Title.TLabel", background=COLOR_BG, foreground=COLOR_TEXT, font=(FONT_FAMILY_UI, 15, "bold"))
        style.configure("Status.TLabel", background=COLOR_BG, foreground=COLOR_TEXT_MUTED, font=(FONT_FAMILY_UI, 9))

        style.configure("TButton", background=COLOR_SURFACE_2, foreground=COLOR_TEXT,
                         font=bold_font, padding=(14, 8), borderwidth=0, focuscolor=COLOR_ACCENT)
        style.map("TButton", background=[("active", COLOR_BORDER)])

        style.configure("Primary.TButton", background=COLOR_ACCENT, foreground="#1a1405",
                         font=bold_font, padding=(14, 8), borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#f6c66a")])

        style.configure("Stop.TButton", background=COLOR_ERROR, foreground="#2a0a0a",
                         font=bold_font, padding=(14, 8), borderwidth=0)
        style.map("Stop.TButton", background=[("active", "#ff8a8a")])

        style.configure("TEntry", fieldbackground=COLOR_SURFACE_2, foreground=COLOR_TEXT,
                         insertcolor=COLOR_TEXT, borderwidth=1, padding=6)
        style.configure("TSpinbox", fieldbackground=COLOR_SURFACE_2, foreground=COLOR_TEXT,
                         arrowsize=12, padding=4)
        style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_TEXT, font=base_font)
        style.map("TRadiobutton", background=[("active", COLOR_BG)])

        style.configure("TNotebook", background=COLOR_BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=COLOR_SURFACE, foreground=COLOR_TEXT_MUTED,
                         padding=(16, 8), font=bold_font, borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", COLOR_SURFACE_2)],
                  foreground=[("selected", COLOR_TEXT)])

        style.configure("Treeview", background=COLOR_SURFACE, fieldbackground=COLOR_SURFACE,
                         foreground=COLOR_TEXT, rowheight=26, borderwidth=0, font=base_font)
        style.configure("Treeview.Heading", background=COLOR_SURFACE_2, foreground=COLOR_TEXT_MUTED,
                         font=bold_font, borderwidth=0)
        style.map("Treeview", background=[("selected", COLOR_ACCENT_2)],
                  foreground=[("selected", "#0F1226")])

        style.configure("Vertical.TScrollbar", background=COLOR_SURFACE_2, troughcolor=COLOR_BG,
                         borderwidth=0, arrowsize=12)

    # -- menu ----------------------------------------------------------------
    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Export alerts...", command=self._export_alerts)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Clear live feed", command=self._clear_feed)
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    # -- layout ----------------------------------------------------------------
    def _build_layout(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        self._build_header(outer)
        self._build_controls(outer)
        self._build_tabs(outer)
        self._build_status_bar(outer)

    def _build_header(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill="x", pady=(0, 14))

        ttk.Label(header, text="Log Watcher", style="Title.TLabel").pack(side="left")

        status_frame = ttk.Frame(header)
        status_frame.pack(side="right")
        self.status_dot = tk.Canvas(status_frame, width=12, height=12, bg=COLOR_BG,
                                     highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 8))
        self._draw_dot(COLOR_TEXT_MUTED)
        self.status_label = ttk.Label(status_frame, text="Idle", style="Muted.TLabel")
        self.status_label.pack(side="left")

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 10, 10, fill=color, outline="")

    def _build_controls(self, parent):
        panel = ttk.Frame(parent, style="Surface.TFrame", padding=14)
        panel.pack(fill="x", pady=(0, 14))

        # Row 1: source selection
        row1 = ttk.Frame(panel, style="Surface.TFrame")
        row1.pack(fill="x", pady=(0, 10))

        self.source_mode = tk.StringVar(value="auto")
        auto_label = f"Auto-detect ({platform.system()} system log)"
        ttk.Radiobutton(row1, text=auto_label, value="auto", variable=self.source_mode,
                         command=self._on_source_mode_change).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(row1, text="Specific file", value="file", variable=self.source_mode,
                         command=self._on_source_mode_change).pack(side="left")

        self.file_path_var = tk.StringVar()
        self.file_entry = ttk.Entry(row1, textvariable=self.file_path_var, width=40, state="disabled")
        self.file_entry.pack(side="left", padx=(14, 6), fill="x", expand=True)
        self.browse_btn = ttk.Button(row1, text="Browse...", command=self._browse_file, state="disabled")
        self.browse_btn.pack(side="left")

        # Row 2: interval, model, start/stop
        row2 = ttk.Frame(panel, style="Surface.TFrame")
        row2.pack(fill="x")

        ttk.Label(row2, text="Poll every", background=COLOR_SURFACE).pack(side="left")
        self.interval_var = tk.IntVar(value=5)
        interval_spin = ttk.Spinbox(row2, from_=1, to=300, width=5, textvariable=self.interval_var)
        interval_spin.pack(side="left", padx=(8, 2))
        ttk.Label(row2, text="sec", background=COLOR_SURFACE).pack(side="left", padx=(0, 20))

        ttk.Label(row2, text="Model", background=COLOR_SURFACE).pack(side="left")
        self.model_var = tk.StringVar(value=agent.MODEL)
        ttk.Entry(row2, textvariable=self.model_var, width=16).pack(side="left", padx=(8, 20))

        self.start_stop_btn = ttk.Button(row2, text="Start watching", style="Primary.TButton",
                                          command=self._toggle_watch)
        self.start_stop_btn.pack(side="right")

    def _on_source_mode_change(self):
        is_file = self.source_mode.get() == "file"
        state = "normal" if is_file else "disabled"
        self.file_entry.configure(state=state)
        self.browse_btn.configure(state=state)

    def _browse_file(self):
        path = filedialog.askopenfilename(title="Choose a log file")
        if path:
            self.file_path_var.set(path)

    def _build_tabs(self, parent):
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True, pady=(0, 10))

        # -- Live feed tab --
        feed_tab = ttk.Frame(self.notebook, padding=0)
        self.notebook.add(feed_tab, text="Live feed")

        self.feed_text = scrolledtext.ScrolledText(
            feed_tab, wrap="word", bg=COLOR_SURFACE, fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT, font=(FONT_FAMILY_MONO, 10),
            borderwidth=0, highlightthickness=0, padx=12, pady=10, state="disabled",
        )
        self.feed_text.pack(fill="both", expand=True)
        for tag, color in [
            ("info", COLOR_TEXT_MUTED), ("raw", COLOR_ACCENT_2), ("warn", COLOR_WARN),
            ("error", COLOR_ERROR), ("critical", COLOR_CRITICAL), ("ok", COLOR_OK),
        ]:
            self.feed_text.tag_configure(tag, foreground=color)
        self.feed_text.tag_configure("critical", font=(FONT_FAMILY_MONO, 10, "bold"))

        # -- Alerts tab --
        alerts_tab = ttk.Frame(self.notebook, padding=0)
        self.notebook.add(alerts_tab, text="Alerts")

        paned = ttk.PanedWindow(alerts_tab, orient="vertical")
        paned.pack(fill="both", expand=True)

        tree_frame = ttk.Frame(paned)
        self.alerts_tree = ttk.Treeview(
            tree_frame, columns=("time", "severity", "summary"), show="headings", height=8
        )
        self.alerts_tree.heading("time", text="Time")
        self.alerts_tree.heading("severity", text="Severity")
        self.alerts_tree.heading("summary", text="Summary")
        self.alerts_tree.column("time", width=90, anchor="w")
        self.alerts_tree.column("severity", width=90, anchor="w")
        self.alerts_tree.column("summary", width=560, anchor="w")
        self.alerts_tree.pack(fill="both", expand=True, side="left")
        self.alerts_tree.bind("<<TreeviewSelect>>", self._on_alert_selected)

        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.alerts_tree.yview)
        self.alerts_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        paned.add(tree_frame, weight=1)

        detail_frame = ttk.Frame(paned, style="Surface.TFrame", padding=12)
        self.alert_detail = tk.Text(
            detail_frame, wrap="word", bg=COLOR_SURFACE, fg=COLOR_TEXT,
            font=(FONT_FAMILY_UI, 10), borderwidth=0, highlightthickness=0, height=10, state="disabled",
        )
        self.alert_detail.pack(fill="both", expand=True)
        paned.add(detail_frame, weight=1)

    def _build_status_bar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        self.cycle_label = ttk.Label(bar, text="Cycles: 0", style="Status.TLabel")
        self.cycle_label.pack(side="left")
        self.failures_label = ttk.Label(bar, text="", style="Status.TLabel")
        self.failures_label.pack(side="left", padx=(20, 0))
        self.last_update_label = ttk.Label(bar, text="", style="Status.TLabel")
        self.last_update_label.pack(side="right")

    # -- feed helpers -----------------------------------------------------
    def _append_feed(self, text: str, tag: str = "info"):
        self.feed_text.configure(state="normal")
        self.feed_text.insert("end", text + "\n", tag)
        self.feed_text.see("end")
        self.feed_text.configure(state="disabled")

    def _clear_feed(self):
        self.feed_text.configure(state="normal")
        self.feed_text.delete("1.0", "end")
        self.feed_text.configure(state="disabled")

    # -- start/stop ---------------------------------------------------------
    def _toggle_watch(self):
        if self.is_watching:
            self._stop_watch()
        else:
            self._start_watch()

    def _start_watch(self):
        config = {
            "mode": self.source_mode.get(),
            "file_path": self.file_path_var.get().strip(),
            "interval": max(1, int(self.interval_var.get() or 5)),
            "model": self.model_var.get().strip(),
        }
        self.stop_event = threading.Event()
        self.worker = WatcherThread(config, self.out_queue, self.stop_event)
        self.worker.start()

        self.is_watching = True
        self.start_stop_btn.configure(text="Stop watching", style="Stop.TButton")
        self._set_status("Starting...", COLOR_WARN)

    def _stop_watch(self):
        if self.worker:
            self.stop_event.set()
        self.is_watching = False
        self.start_stop_btn.configure(text="Start watching", style="Primary.TButton")
        self._set_status("Idle", COLOR_TEXT_MUTED)

    def _set_status(self, text: str, color: str):
        self.status_label.configure(text=text)
        self._draw_dot(color)

    # -- queue processing (runs on the main/UI thread) ----------------------
    def _poll_queue(self):
        try:
            while True:
                event = self.out_queue.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _handle_event(self, event: dict):
        kind = event["kind"]
        ts = event["time"]

        if kind == "started":
            self._set_status(f"Watching ({event['model']})", COLOR_ACCENT)
            self._append_feed(f"[{ts}] Started watching — model: {event['model']}", "ok")

        elif kind == "stopped":
            self._append_feed(f"[{ts}] Stopped.", "info")

        elif kind == "source_error":
            self._append_feed(f"[{ts}] Could not start: {event['message']}", "critical")
            messagebox.showerror("Log Watcher", event["message"])
            self._stop_watch()

        elif kind == "source_failure":
            self._append_feed(f"[{ts}] Log source error (attempt {event['count']}): {event['message']}", "error")

        elif kind == "entries":
            for e in event["entries"]:
                sev = e.get("severity", "warning")
                tag = "critical" if sev in ("emergency", "alert", "critical", "fault") else \
                      "error" if sev == "error" else "warn"
                self._append_feed(f"[{ts}] [{sev}] {e.get('source', '')}: {e.get('message', '')[:200]}", tag)

        elif kind == "analyzing":
            self._append_feed(f"[{ts}] Asking model to explain {event['n']} new entr{'y' if event['n']==1 else 'ies'}...", "info")

        elif kind == "routine":
            self._append_feed(f"[{ts}] Routine — {event['summary']}", "ok")

        elif kind == "alert":
            self._handle_alert(ts, event["result"], event["entries"])

        elif kind == "explain_failure":
            self._append_feed(f"[{ts}] Explanation failed (attempt {event['count']}): {event['message']}", "error")

        elif kind == "monitor_failure":
            self._append_feed(f"[{ts}] MONITOR ITSELF IS FAILING ({event['count']}x): {event['message']}", "critical")
            self._set_status("Monitor failing", COLOR_CRITICAL)

        elif kind == "cycle":
            self.cycle_label.configure(text=f"Cycles: {event['n']}")
            self.last_update_label.configure(text=f"Last poll: {ts}")

    def _handle_alert(self, ts: str, result: dict, entries: list):
        severity = result.get("severity", "warning")
        summary = result.get("summary", "")
        self._append_feed(f"[{ts}] ALERT ({severity}): {summary}", "critical")

        agent.notify("System Log Alert", summary or "Check the Alerts tab for details.")
        try:
            with open(agent.ALERT_LOG, "a") as f:
                f.write(json.dumps({"time": ts, "result": result, "raw_entries": entries}) + "\n")
        except OSError:
            pass

        record_index = len(self.alert_records)
        self.alert_records.append({"time": ts, "result": result, "entries": entries})
        self.alerts_tree.insert("", "end", iid=str(record_index),
                                 values=(ts, severity, summary[:100]))
        self.notebook.tab(1, text=f"Alerts ({len(self.alert_records)})")

    def _on_alert_selected(self, _event):
        selection = self.alerts_tree.selection()
        if not selection:
            return
        record = self.alert_records[int(selection[0])]
        result = record["result"]

        lines = [f"Severity: {result.get('severity', 'unknown')}", f"Summary: {result.get('summary', '')}", ""]
        for item in result.get("explanations", []):
            if isinstance(item, dict):
                lines.append(f"Log:    {str(item.get('message', ''))[:200]}")
                lines.append(f"Means:  {item.get('meaning', '')}")
                lines.append(f"Action: {item.get('action', '')}")
            else:
                lines.append(f"- {item}")
            lines.append("")

        self.alert_detail.configure(state="normal")
        self.alert_detail.delete("1.0", "end")
        self.alert_detail.insert("1.0", "\n".join(lines))
        self.alert_detail.configure(state="disabled")

    # -- menu actions -----------------------------------------------------
    def _export_alerts(self):
        if not self.alert_records:
            messagebox.showinfo("Log Watcher", "No alerts to export yet.")
            return
        path = filedialog.asksaveasfilename(
            title="Export alerts", defaultextension=".json",
            filetypes=[("JSON", "*.json")], initialfile="log_alerts_export.json",
        )
        if not path:
            return
        with open(path, "w") as f:
            json.dump(self.alert_records, f, indent=2, default=str)
        messagebox.showinfo("Log Watcher", f"Exported {len(self.alert_records)} alert(s) to {path}")

    def _show_about(self):
        messagebox.showinfo(
            "About Log Watcher",
            "Log Watcher\n\n"
            "A GUI for log_watcher_agent.py — watches system or application "
            "logs, uses a local Ollama model to explain warnings/errors in "
            "plain language, and alerts you when something needs attention.",
        )

    def _on_close(self):
        if self.is_watching:
            self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    app = LogWatcherApp()
    app.mainloop()
