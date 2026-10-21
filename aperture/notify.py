"""On-screen notifications, because a silent guard is indistinguishable from a broken one.

This module exists because of a real failure. The guard worked perfectly -- it caught an
access key on a real screenshot, scored it 0.900, and blacked out 56% of the frame -- and
the person using it reported that "nothing happened". The only feedback was a console log
in a terminal they could not see. A tool that silently rewrites your clipboard and tells
you nothing has no business being trusted.

Two notifications, because there are two moments worth announcing:

    hold()     the instant the clipboard is taken, so the pause is explained rather
               than mysterious. On a CPU the gate takes a minute; without this, paste
               simply stops working for no visible reason.
    verdict()  what was decided, with the numbers.

**Why a subprocess rather than a thread.** Tkinter wants to own the main thread, and the
guard loop is already there. Spawning a short-lived child per notification means a
mis-sized window or a dead display cannot take the guard down with it, and the parent can
kill the "checking" panel the moment the verdict is ready. Fire and forget.

No new dependencies: tkinter ships with Python on Windows and macOS. If it is missing
(a headless Linux box, a stripped install) the child degrades to a console bell and the
guard carries on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field

# Accent colours by kind. Red for anything withheld, amber while thinking, green for a
# clean pass -- the usual traffic light, because this has to be readable at a glance and
# in a compressed video.
ACCENTS = {
    "hold": "#e8a33d",
    "redact": "#e0453f",
    "withhold": "#e0453f",
    "pass": "#2faa5f",
    "info": "#4a90d9",
}

BACKGROUND = "#15151a"
FOREGROUND = "#ececf2"
MUTED = "#9a9aa8"

# Long enough to read, short enough not to be in the way.
DEFAULT_SECONDS = 7.0
# The hold panel stays until the verdict replaces it, but not forever: if the gate dies
# the panel should not outlive it.
HOLD_SECONDS = 180.0


@dataclass
class Notifier:
    """Spawns notification windows, and keeps track of the one still on screen."""

    enabled: bool = True
    _current: subprocess.Popen | None = field(default=None, repr=False)

    def _spawn(self, payload: dict) -> subprocess.Popen | None:
        if not self.enabled:
            return None
        try:
            return subprocess.Popen(
                [sys.executable, "-m", "aperture.notify", json.dumps(payload)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Detach on Windows so the panel is not tied to the console's lifetime.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001 - a notification must never break the guard
            return None

    def dismiss(self) -> None:
        if self._current and self._current.poll() is None:
            try:
                self._current.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._current = None

    def hold(self, size: tuple[int, int]) -> None:
        """Announce that the clipboard has been taken, before anything is known."""
        self.dismiss()
        self._current = self._spawn({
            "kind": "hold",
            "title": "Checking this screenshot",
            "lines": [
                f"{size[0]}x{size[1]} image held",
                "Paste is paused until this finishes.",
                "Inspecting locally -- nothing has been sent.",
            ],
            "seconds": HOLD_SECONDS,
        })

    def verdict(
        self,
        *,
        kind: str,
        title: str,
        lines: list[str],
        seconds: float = DEFAULT_SECONDS,
    ) -> None:
        self.dismiss()
        self._current = self._spawn({
            "kind": kind,
            "title": title,
            "lines": lines,
            "seconds": seconds,
        })


# --------------------------------------------------------------------------- the window


def _show(payload: dict) -> int:
    """Draw the panel. Runs in the child process only."""
    try:
        import tkinter as tk
    except ImportError:
        # No display toolkit. A bell is not much, but it is not silence.
        print("\a", end="", flush=True)
        print(f"{payload.get('title')}: {'; '.join(payload.get('lines', []))}")
        return 0

    kind = payload.get("kind", "info")
    accent = ACCENTS.get(kind, ACCENTS["info"])
    title = payload.get("title", "Aperture")
    lines = payload.get("lines", [])
    seconds = float(payload.get("seconds", DEFAULT_SECONDS))

    root = tk.Tk()
    root.overrideredirect(True)  # no title bar: this is a panel, not a window
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", 0.97)
    except tk.TclError:
        pass

    width = 430
    height = 108 + 22 * len(lines)

    # Bottom-right, clear of the Windows notification area.
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    root.geometry(f"{width}x{height}+{screen_w - width - 28}+{screen_h - height - 72}")

    frame = tk.Frame(root, bg=BACKGROUND, highlightthickness=0)
    frame.pack(fill="both", expand=True)

    # A thick accent stripe down the left edge carries the verdict at a glance, which
    # matters more than the text when this flashes past in a screen recording.
    tk.Frame(frame, bg=accent, width=7).pack(side="left", fill="y")

    body = tk.Frame(frame, bg=BACKGROUND)
    body.pack(side="left", fill="both", expand=True, padx=18, pady=14)

    tk.Label(
        body, text="APERTURE", bg=BACKGROUND, fg=accent,
        font=("Segoe UI", 8, "bold"), anchor="w",
    ).pack(fill="x")
    tk.Label(
        body, text=title, bg=BACKGROUND, fg=FOREGROUND,
        font=("Segoe UI", 13, "bold"), anchor="w", justify="left",
        wraplength=width - 70,
    ).pack(fill="x", pady=(2, 6))

    for line in lines:
        tk.Label(
            body, text=line, bg=BACKGROUND, fg=MUTED,
            font=("Segoe UI", 9), anchor="w", justify="left",
            wraplength=width - 70,
        ).pack(fill="x")

    # Clicking it dismisses it, because an overlay you cannot get rid of is worse than
    # no overlay.
    for widget in (root, frame, body):
        widget.bind("<Button-1>", lambda _event: root.destroy())

    root.after(int(seconds * 1000), root.destroy)
    try:
        root.mainloop()
    except Exception:  # noqa: BLE001
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        # Useful for checking the panel renders without running the whole guard.
        return _show({
            "kind": "redact",
            "title": "Redacted 2 regions",
            "lines": ["credential 0.90 - top left", "56% of the frame blacked out",
                      "Safe copy is on your clipboard."],
            "seconds": 6,
        })
    try:
        payload = json.loads(args[0])
    except json.JSONDecodeError:
        return 1
    return _show(payload)


if __name__ == "__main__":
    sys.exit(main())
