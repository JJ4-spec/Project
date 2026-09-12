#!/usr/bin/env python3
"""
System log monitor + Ollama explainer agent.

Polls your system's logs on a short interval, pulls out warning/error/critical
entries, and asks a local model to explain what they mean in plain language
and whether they're worth acting on. Designed to run continuously in a
terminal (or as a background service — see notes at the bottom).

Sources supported:
  - Linux:   journalctl (systemd journal), priority warning and above
  - macOS:   `log show` (unified logging), messageType Error/Fault
  - Any OS:  a plain text log file, tailed and filtered by keyword
             (use --file /path/to/app.log if you want to watch a specific
             app's log instead of/alongside the OS log)

It also watches its OWN health: if the log source or the model becomes
unreachable for several cycles in a row, it alerts you about THAT too,
instead of silently going quiet.

Requirements:
    pip install requests --break-system-packages
    Linux:  systemd (journalctl) — present on most modern distros
    macOS:  `log` command — built in
    Ollama running locally with a model pulled

Usage:
    python3 log_watcher_agent.py                       # auto-detect OS log source
    python3 log_watcher_agent.py --interval 10
    python3 log_watcher_agent.py --file /var/log/myapp.log
    python3 log_watcher_agent.py --once                 # single poll, for testing
"""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from collections import deque

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:0.6b"          # swap for a larger model if explanations feel too shallow
ALERT_LOG = "log_alerts.log"
FAILURE_ALERT_THRESHOLD = 3    # consecutive failures before we alert about the monitor itself
DEDUP_WINDOW_SECONDS = 300     # suppress identical repeated messages within this window

FILE_TAIL_KEYWORDS = ["error", "critical", "fatal", "panic", "emerg", "warn"]


# ---------------------------------------------------------------------------
# Log sources — each yields a list of {"timestamp", "severity", "message"} dicts
# for entries newer than what was already seen.
# ---------------------------------------------------------------------------
class JournalctlSource:
    """Linux systemd journal, priority 'warning' and above."""

    def __init__(self):
        self.since = time.strftime("%Y-%m-%d %H:%M:%S")

    def poll(self) -> list[dict]:
        cmd = ["journalctl", "-o", "json", "-p", "warning", "--since", self.since, "--no-pager"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"journalctl failed: {result.stderr.strip()}")

        entries = []
        latest_ts = None
        for line in result.stdout.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            us = int(rec.get("__REALTIME_TIMESTAMP", 0))
            ts = us / 1_000_000
            latest_ts = max(latest_ts or ts, ts)
            entries.append({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                "severity": priority_to_name(rec.get("PRIORITY", "4")),
                "message": rec.get("MESSAGE", "").strip() if isinstance(rec.get("MESSAGE"), str) else str(rec.get("MESSAGE", "")),
                "source": rec.get("_SYSTEMD_UNIT") or rec.get("SYSLOG_IDENTIFIER") or "system",
            })
        if latest_ts:
            self.since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_ts + 0.001))
        return entries


def priority_to_name(p) -> str:
    names = {"0": "emergency", "1": "alert", "2": "critical", "3": "error", "4": "warning"}
    return names.get(str(p), "warning")


class MacLogSource:
    """macOS unified logging, Error/Fault entries."""

    def __init__(self, lookback_seconds: int):
        self.lookback_seconds = lookback_seconds
        self.seen_hashes = set()

    def poll(self) -> list[dict]:
        cmd = [
            "log", "show", "--style", "ndjson",
            "--last", f"{self.lookback_seconds}s",
            "--predicate", 'messageType == "Error" OR messageType == "Fault"',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"log show failed: {result.stderr.strip()}")

        entries = []
        for line in result.stdout.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("eventMessage", "").strip()
            if not msg:
                continue
            sig = hashlib.sha1((rec.get("timestamp", "") + msg).encode()).hexdigest()
            if sig in self.seen_hashes:
                continue
            self.seen_hashes.add(sig)
            entries.append({
                "timestamp": rec.get("timestamp", ""),
                "severity": "fault" if rec.get("messageType") == "Fault" else "error",
                "message": msg,
                "source": rec.get("processImagePath", "system").split("/")[-1],
            })
        # keep the hash set from growing forever
        if len(self.seen_hashes) > 5000:
            self.seen_hashes = set(list(self.seen_hashes)[-2000:])
        return entries


class FileTailSource:
    """Generic text log file, filtered by keyword. Works on any OS."""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, "r", errors="ignore") as f:
                f.seek(0, 2)
                self.offset = f.tell()
        except FileNotFoundError:
            raise RuntimeError(f"Log file not found: {path}")

    def poll(self) -> list[dict]:
        entries = []
        with open(self.path, "r", errors="ignore") as f:
            f.seek(self.offset)
            new_lines = f.readlines()
            self.offset = f.tell()

        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            lower = line.lower()
            if any(kw in lower for kw in FILE_TAIL_KEYWORDS):
                severity = "critical" if any(k in lower for k in ("fatal", "panic", "emerg", "critical")) else \
                           "error" if "error" in lower else "warning"
                entries.append({
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "severity": severity,
                    "message": line,
                    "source": self.path,
                })
        return entries


def build_source(args):
    if args.file:
        return FileTailSource(args.file)
    system = platform.system()
    if system == "Linux":
        return JournalctlSource()
    if system == "Darwin":
        return MacLogSource(lookback_seconds=args.interval + 5)
    print("No built-in OS log source for this platform. Use --file to point at a "
          "specific log file instead (e.g. an application log).")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Deduplication — avoid re-alerting on the exact same repeated message
# ---------------------------------------------------------------------------
class Deduper:
    def __init__(self, window_seconds: int):
        self.window_seconds = window_seconds
        self.recent: dict[str, float] = {}

    def is_new(self, message: str) -> bool:
        sig = hashlib.sha1(message.encode()).hexdigest()
        now = time.time()
        last_seen = self.recent.get(sig)
        self.recent[sig] = now
        if last_seen is not None and (now - last_seen) < self.window_seconds:
            return False
        return True

    def cleanup(self):
        cutoff = time.time() - self.window_seconds
        self.recent = {k: v for k, v in self.recent.items() if v > cutoff}


# ---------------------------------------------------------------------------
# Model explanation
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a patient, plain-language system administrator explaining log
entries to someone who is not a Linux/macOS internals expert. For the batch of log
entries you're given, explain in simple terms what's going on, whether it's something
to worry about, and if so what a reasonable next step would be. Skip entries that are
routine noise. Respond with ONLY a JSON object, no other text, in this exact shape:
{"alert": true or false, "severity": "info" | "warning" | "critical",
 "summary": "one or two plain-English sentences",
 "explanations": [{"message": "...", "meaning": "plain-language explanation", "action": "what to do, or 'no action needed'"}]}
If everything looks like routine/harmless noise, return {"alert": false, "severity": "info", "summary": "...", "explanations": []}."""


def explain_entries(entries: list[dict]) -> dict:
    prompt = "Log entries to review:\n" + json.dumps(entries, indent=2)
    resp = requests.post(OLLAMA_URL, json={
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }, timeout=120)
    resp.raise_for_status()
    content = resp.json().get("message", {}).get("content", "").strip()

    cleaned = content.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse model response: {content[:200]!r}")


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
def notify(title: str, body: str):
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                             f'display notification "{body[:200]}" with title "{title}"'], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", title, body[:200]], check=False)
    except FileNotFoundError:
        pass


def alert_anomaly(result: dict, raw_entries: list[dict]):
    banner = "!" * 60
    print(f"\n{banner}\nLOG ALERT — severity: {result.get('severity', 'unknown')}\n{banner}")
    print(result.get("summary", ""))
    for item in result.get("explanations", []):
        print(f"\n  Log:    {item.get('message', '')[:200]}")
        print(f"  Means:  {item.get('meaning', '')}")
        print(f"  Action: {item.get('action', '')}")
    print()

    with open(ALERT_LOG, "a") as f:
        f.write(json.dumps({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "result": result,
            "raw_entries": raw_entries,
        }) + "\n")

    notify("System Log Alert", result.get("summary", "Check the terminal for details."))


def alert_monitor_failure(reason: str, consecutive: int):
    msg = f"Log monitor has failed {consecutive} times in a row: {reason}"
    print(f"\n{'!'*60}\nMONITOR ITSELF IS FAILING\n{'!'*60}\n{msg}\n")
    with open(ALERT_LOG, "a") as f:
        f.write(json.dumps({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "monitor_failure": msg,
        }) + "\n")
    notify("Log Monitor Broken", msg)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="System log monitor + plain-language explainer")
    parser.add_argument("--interval", type=int, default=5, help="Seconds between polls (default 5)")
    parser.add_argument("--file", help="Path to a specific log file to tail instead of the OS log")
    parser.add_argument("--once", action="store_true", help="Run a single poll and exit (for testing)")
    args = parser.parse_args()

    try:
        source = build_source(args)
    except RuntimeError as e:
        print(f"Could not start log source: {e}")
        sys.exit(1)

    deduper = Deduper(DEDUP_WINDOW_SECONDS)
    consecutive_failures = 0
    failure_reason = ""
    cycle = 0

    print(f"Watching logs every {args.interval}s "
          f"({'file: ' + args.file if args.file else platform.system() + ' system log'}). "
          f"Ctrl+C to stop.\n")

    while True:
        cycle += 1
        try:
            raw_entries = source.poll()
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            failure_reason = str(e)
            print(f"[cycle {cycle}] log source error: {failure_reason}")
            if consecutive_failures == FAILURE_ALERT_THRESHOLD:
                alert_monitor_failure(failure_reason, consecutive_failures)
            if args.once:
                break
            time.sleep(args.interval)
            continue

        new_entries = [e for e in raw_entries if deduper.is_new(e["message"])]
        deduper.cleanup()

        if not new_entries:
            print(f"[cycle {cycle}] no new notable log entries")
        else:
            print(f"[cycle {cycle}] {len(new_entries)} new entries, asking model to explain...")
            try:
                result = explain_entries(new_entries)
                consecutive_failures = 0
                if result.get("alert"):
                    alert_anomaly(result, new_entries)
                else:
                    print(f"[cycle {cycle}] model judged these routine: {result.get('summary', '')}")
            except (requests.RequestException, ValueError) as e:
                consecutive_failures += 1
                failure_reason = f"model explanation failed: {e}"
                print(f"[cycle {cycle}] {failure_reason}")
                if consecutive_failures == FAILURE_ALERT_THRESHOLD:
                    alert_monitor_failure(failure_reason, consecutive_failures)

        if args.once:
            break
        time.sleep(args.interval)

    print("Stopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
