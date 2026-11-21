"""Faugus X launch-status bridge.

When Faugus X (Showcase mode) launches a game it sets FAUGUS_X_STATUS_DIR.
The runner then uses a StatusBridge in place of its splash-window subprocess:
the same text commands the splash understands (LABEL / PULSE / FRACTION / HIDE)
are written as a small JSON state file that Faugus X's launch screen polls.
Closing the bridge (the runner does this when the game process appears)
marks the launch as finished.
"""

import json
import os
import time

STATUS_DIR_ENV = "FAUGUS_X_STATUS_DIR"


def status_path(status_dir, gameid):
    return os.path.join(status_dir, f"{gameid}.json")


def read_status(status_dir, gameid):
    try:
        with open(status_path(status_dir, gameid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class _StatusWriter:
    def __init__(self, path):
        self.path = path
        self.state = {"label": "", "fraction": None, "pulse": False, "closed": False, "time": time.time()}
        self._dump()

    def write(self, text):
        for line in text.splitlines():
            cmd, _sep, arg = line.partition(" ")
            if cmd == "LABEL":
                self.state["label"] = arg
            elif cmd == "PULSE":
                self.state["pulse"] = True
                self.state["fraction"] = None
            elif cmd == "FRACTION":
                try:
                    self.state["fraction"] = max(0.0, min(1.0, float(arg)))
                    self.state["pulse"] = False
                except ValueError:
                    pass
            elif cmd == "HIDE":
                self.state["pulse"] = False
                self.state["fraction"] = None
        self._dump()

    def flush(self):
        pass

    def close(self):
        self.state["closed"] = True
        self._dump()

    def _dump(self):
        self.state["time"] = time.time()
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.path)
        except OSError:
            pass


class StatusBridge:
    """Duck-types the splash subprocess used by FaugusRun (stdin + wait)."""

    def __init__(self, path):
        self.stdin = _StatusWriter(path)

    def wait(self):
        return 0


def bridge_for(gameid):
    status_dir = os.environ.get(STATUS_DIR_ENV)
    if not status_dir or not gameid:
        return None
    try:
        os.makedirs(status_dir, exist_ok=True)
    except OSError:
        return None
    return StatusBridge(status_path(status_dir, gameid))
