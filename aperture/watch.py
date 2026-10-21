"""Watching the clipboard, fail-closed.

This is the part that addresses the real case: you screenshot a terminal error, the frame
also catches a credentials file, and you paste it into a chat window. No agent is
involved, so no intervention handler can help -- the leak happens upstream of any tool
call. The only place to stand is between the screenshot and the paste.

The sequence, and the order matters:

    1. an image appears on the clipboard
    2. we take it away immediately and leave a placeholder
    3. the fast lane runs (OCR + regex, well under a second)
    4. the judgement lane runs (Decider, seconds on CPU)
    5. we put back a redacted image, or the original if it was clean

Step 2 is the whole design. Inspecting first and replacing after would leave a window in
which a paste gets the original, and on a CPU that window is ten seconds wide. Holding it
first means you cannot leak what you cannot paste. The cost is that paste is briefly
unavailable, which is a trade worth making and is stated plainly in the status line.

Scope, deliberately narrow:

* images only. Text on your clipboard is never read. Most of the reason to distrust a
  clipboard tool is that it reads your passwords as you copy them; this one cannot.
* only while this command runs. No service, no autostart, no tray icon.
* nothing is written to disk. Verdicts and numbers go to the console; pixels do not go
  anywhere.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from PIL import Image

from .decider import Decider
from .patterns import Policy, RegionGate, Verdict
from .questions import CONTEXT_RULES, REGION_RULES, SCREEN_KIND
from .redact import hold_placeholder, redact
from .scanner import ocr_available, scan

POLL_SECONDS = 0.4


# --------------------------------------------------------------------------- platform


class ClipboardBackend(Protocol):
    """The two operations a platform has to provide."""

    def get_image(self) -> Image.Image | None: ...
    def set_image(self, image: Image.Image) -> None: ...
    @property
    def name(self) -> str: ...


class WindowsClipboard:
    """Windows clipboard via pywin32, using the DIB format.

    PIL can read an image off the clipboard but cannot write one back, so the write side
    builds a BMP and strips its 14-byte file header to produce a DIB, which is what
    CF_DIB expects.
    """

    name = "Windows (pywin32, CF_DIB)"

    def __init__(self) -> None:
        import win32clipboard  # noqa: F401  - fail here if it is missing

        self._win32clipboard = win32clipboard

    def get_image(self) -> Image.Image | None:
        from PIL import ImageGrab

        try:
            grabbed = ImageGrab.grabclipboard()
        except Exception:  # noqa: BLE001 - a transient clipboard lock is not fatal
            return None
        if isinstance(grabbed, Image.Image):
            return grabbed
        return None

    def set_image(self, image: Image.Image) -> None:
        import io

        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="BMP")
        dib = buf.getvalue()[14:]  # drop the BITMAPFILEHEADER

        clip = self._win32clipboard
        for attempt in range(6):
            try:
                clip.OpenClipboard()
            except Exception:  # noqa: BLE001 - another process holds it; back off
                time.sleep(0.08 * (attempt + 1))
                continue
            try:
                clip.EmptyClipboard()
                clip.SetClipboardData(clip.CF_DIB, dib)
            finally:
                clip.CloseClipboard()
            return
        raise RuntimeError("could not take the clipboard after several attempts")


class MacClipboard:
    """macOS clipboard via NSPasteboard.

    UNTESTED. Written against the AppKit API but never run on hardware, because this was
    developed on Windows. See the README. Reports welcome.
    """

    name = "macOS (NSPasteboard) -- UNTESTED"

    def __init__(self) -> None:
        from AppKit import NSPasteboard  # noqa: F401

        from AppKit import NSPasteboard as _NSPasteboard

        self._pasteboard = _NSPasteboard.generalPasteboard()

    def get_image(self) -> Image.Image | None:
        import io

        from AppKit import NSPasteboardTypePNG, NSPasteboardTypeTIFF

        for type_ in (NSPasteboardTypePNG, NSPasteboardTypeTIFF):
            data = self._pasteboard.dataForType_(type_)
            if data:
                return Image.open(io.BytesIO(bytes(data.bytes())))
        return None

    def set_image(self, image: Image.Image) -> None:
        import io

        from AppKit import NSData, NSPasteboardTypePNG

        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        data = NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))
        self._pasteboard.clearContents()
        self._pasteboard.setData_forType_(data, NSPasteboardTypePNG)


def get_clipboard() -> ClipboardBackend:
    if sys.platform == "win32":
        return WindowsClipboard()
    if sys.platform == "darwin":
        return MacClipboard()
    raise RuntimeError(f"no clipboard backend for {sys.platform}")


def clipboard_backend() -> str:
    return get_clipboard().name


# --------------------------------------------------------------------------- the guard


@dataclass
class Tally:
    seen: int = 0
    passed: int = 0
    redacted: int = 0
    withheld: int = 0
    secrets: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"{self.seen} images | {self.passed} passed, {self.redacted} redacted, "
            f"{self.withheld} withheld"
        )


def _fingerprint(image: Image.Image) -> tuple:
    """Cheap identity for an image, to avoid re-inspecting what we just wrote back.

    Size plus a coarse thumbnail of the pixels: enough to tell one screenshot from
    another without hashing megabytes on every poll.
    """
    small = image.convert("L").resize((16, 16), Image.BILINEAR)
    return (image.size, small.tobytes())


def guard_clipboard(
    *,
    decider_url: str | None = None,
    minutes: int = 0,
    columns: int = 2,
    rows: int = 2,
    mode: str = "block",
    use_ocr: bool = True,
) -> int:
    """Watch the clipboard until Ctrl+C, or until `minutes` have passed."""
    clipboard = get_clipboard()
    decider = Decider(decider_url)
    info = decider.require_vision()
    gate = RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        info_questions=SCREEN_KIND,
        policy=Policy(fail_closed=True),
        columns=columns,
        rows=rows,
    )

    ocr_on = use_ocr and ocr_available()
    deadline = time.time() + minutes * 60 if minutes else None
    tally = Tally()
    ignore: set[tuple] = set()

    print("=" * 72)
    print("  APERTURE ARMED")
    print("=" * 72)
    print(f"  clipboard : {clipboard.name}")
    print(f"  decider   : {info.get('checkpoint')} on {info.get('device')}")
    print(f"  fast lane : {'OCR + regex' if ocr_on else 'off (no OCR installed)'}")
    print(f"  grid      : {columns}x{rows} regions, redaction mode '{mode}'")
    if deadline:
        print(f"  expires   : in {minutes} minutes")
    print()
    print("  images only -- text on the clipboard is never read")
    print("  nothing is written to disk; no autostart, no service")
    print("  Ctrl+C to disarm")
    print("=" * 72)
    print()

    try:
        while deadline is None or time.time() < deadline:
            time.sleep(POLL_SECONDS)
            image = clipboard.get_image()
            if image is None:
                continue
            key = _fingerprint(image)
            if key in ignore:
                continue
            ignore.add(key)

            tally.seen += 1

            # Read the clock on every line rather than once per image. The gate takes
            # tens of seconds on a CPU, and stamping every line with the detection time
            # makes that look instantaneous, which is the opposite of the truth.
            def stamped(message: str) -> None:
                print(f"[{time.strftime('%H:%M:%S')}] {message}")

            stamped(f"image on clipboard, {image.width}x{image.height}")

            # 2. Take it away first. This is the fail-closed step.
            try:
                clipboard.set_image(
                    hold_placeholder(image.size, "checking this image, one moment")
                )
                stamped("held -- paste is blocked while we look")
            except Exception as exc:  # noqa: BLE001
                stamped(f"WARNING could not hold the clipboard: {exc}")

            # 3. Fast lane: exact, instant, gives coordinates.
            hints: list[tuple[int, int, int, int]] = []
            if ocr_on:
                started = time.perf_counter()
                secrets = scan(image)
                elapsed = (time.perf_counter() - started) * 1000
                if secrets:
                    for secret in secrets:
                        stamped(f"regex  {secret.kind} ({secret.redacted_sample}) at {secret.box}")
                        tally.secrets.append(secret.kind)
                    hints = [s.box for s in secrets]
                stamped(f"fast lane done in {elapsed:.0f} ms")

            # 4. Judgement lane.
            result = gate.inspect(image, hint_boxes=hints)
            for name, value in result.context.items():
                if value >= 0.3:
                    stamped(f"decider {name}={value:.3f}")
            for finding in result.findings:
                stamped(f"decider {finding}")

            # 5. Put back whatever is safe.
            boxes = list(hints) + result.boxes()
            if result.verdict is Verdict.WITHHOLD or result.verdict is Verdict.UNREADABLE:
                tally.withheld += 1
                replacement = hold_placeholder(
                    image.size,
                    "withheld: " + (result.reasons[0] if result.reasons else "unsafe"),
                )
                clipboard.set_image(replacement)
                stamped(f"WITHHELD  {chr(44).join(result.reasons)}")
            elif boxes:
                tally.redacted += 1
                redaction = redact(image, boxes, mode=mode)  # type: ignore[arg-type]
                clipboard.set_image(redaction.image)
                ignore.add(_fingerprint(redaction.image))
                stamped(f"REDACTED  {redaction.count} region(s), {redaction.fraction_of(image):.0%} of the frame")
            else:
                tally.passed += 1
                clipboard.set_image(image)
                stamped("PASSED    nothing found")

            stamped(f"{result.decider_calls} local decisions, {result.decider_ms:.0f} ms | {tally.line()}")
            print()
    except KeyboardInterrupt:
        pass

    print()
    print("=" * 72)
    print(f"  DISARMED -- {tally.line()}")
    if tally.secrets:
        kinds = ", ".join(sorted(set(tally.secrets)))
        print(f"  structured secrets caught: {kinds}")
    print(f"  {decider}")
    print("=" * 72)
    return 0


# --------------------------------------------------------------------------- files
#
# The other half of the promise. A screenshot does not always go to the clipboard: on
# Windows, Win+PrtScn writes a PNG into Pictures\Screenshots, and the Snipping Tool's
# "Save as" writes wherever you point it. On macOS, Cmd+Shift+4 writes to the Desktop.
# Those files then get attached to an email or dragged into a chat window, and the
# clipboard guard never sees them.
#
# Watching the clipboard alone would leave that hole open, so this watches the folder
# too. Same gate, same two lanes, different trigger.


def default_screenshot_dirs() -> list[Path]:
    """Where this platform puts screenshots, for the directories that exist."""
    home = Path.home()
    if sys.platform == "win32":
        candidates = [
            home / "Pictures" / "Screenshots",
            home / "OneDrive" / "Pictures" / "Screenshots",
            home / "Pictures" / "Screen Shots",
        ]
    elif sys.platform == "darwin":
        candidates = [home / "Desktop", home / "Pictures" / "Screenshots"]
    else:
        candidates = [home / "Pictures", home / "Desktop"]
    return [p for p in candidates if p.is_dir()]


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _settled(path: Path, tries: int = 12, pause: float = 0.25) -> bool:
    """Wait until a file stops growing.

    A screenshot appears on disk before it is finished being written, and reading it
    half-written gives a truncated image that the gate would have to guess about. Two
    equal sizes in a row is good enough.
    """
    last = -1
    for _ in range(tries):
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size > 0 and size == last:
            return True
        last = size
        time.sleep(pause)
    return False


def guard_folders(
    *,
    directories: Sequence[Path] | None = None,
    decider_url: str | None = None,
    minutes: int = 0,
    columns: int = 2,
    rows: int = 2,
    mode: str = "block",
    use_ocr: bool = True,
    replace: bool = False,
) -> int:
    """Watch screenshot folders and redact new captures that are not safe to share.

    `replace` decides what happens to a file that needs redacting:

      False (default)  the original is left alone and a `.safe.png` copy is written
                       beside it. Non-destructive, and you choose which to attach.
      True             the original is overwritten in place, and the unredacted
                       version is gone. Closes the hole properly, at the cost of
                       destroying data the user may have wanted.

    The default is the cautious one, because silently overwriting somebody's files is a
    worse failure than leaving a redacted copy next to them. The README says which.
    """
    watched = list(directories) if directories else default_screenshot_dirs()
    if not watched:
        print("no screenshot folders found to watch")
        return 1

    decider = Decider(decider_url)
    info = decider.require_vision()
    gate = RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        info_questions=SCREEN_KIND,
        policy=Policy(fail_closed=True),
        columns=columns,
        rows=rows,
    )
    ocr_on = use_ocr and ocr_available()
    deadline = time.time() + minutes * 60 if minutes else None
    tally = Tally()

    # Everything already present is ignored. The guard reacts to new captures; it does
    # not go rummaging through your existing pictures.
    seen: set[Path] = set()
    for directory in watched:
        seen.update(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        )

    print("=" * 72)
    print("  APERTURE ARMED (folders)")
    print("=" * 72)
    for directory in watched:
        print(f"  watching  : {directory}")
    print(f"  decider   : {info.get('checkpoint')} on {info.get('device')}")
    print(f"  fast lane : {'OCR + regex' if ocr_on else 'off (no OCR installed)'}")
    print(f"  on a hit  : {'overwrite the original' if replace else 'write a .safe.png copy'}")
    print(f"  ignoring  : {len(seen)} files already present")
    if deadline:
        print(f"  expires   : in {minutes} minutes")
    print()
    print("  only files created from now on; existing pictures are never read")
    print("  Ctrl+C to disarm")
    print("=" * 72)
    print()

    def stamped(message: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {message}")

    try:
        while deadline is None or time.time() < deadline:
            time.sleep(POLL_SECONDS * 2)
            for directory in watched:
                try:
                    entries = list(directory.iterdir())
                except OSError:
                    continue
                for path in entries:
                    if (
                        path in seen
                        or not path.is_file()
                        or path.suffix.lower() not in IMAGE_SUFFIXES
                        or path.name.endswith(".safe.png")
                    ):
                        continue
                    seen.add(path)
                    if not _settled(path):
                        stamped(f"{path.name}: still being written, skipped")
                        continue

                    try:
                        image = Image.open(path)
                        image.load()
                    except Exception as exc:  # noqa: BLE001
                        stamped(f"{path.name}: could not be read ({exc})")
                        continue

                    tally.seen += 1
                    stamped(f"new file {path.name}, {image.width}x{image.height}")

                    hints: list[tuple[int, int, int, int]] = []
                    if ocr_on:
                        for secret in scan(image):
                            stamped(f"regex  {secret.kind} "
                                    f"({secret.redacted_sample}) at {secret.box}")
                            tally.secrets.append(secret.kind)
                            hints.append(secret.box)

                    result = gate.inspect(image, hint_boxes=hints)
                    for finding in result.findings:
                        stamped(f"decider {finding}")

                    boxes = list(hints) + result.boxes()
                    if result.verdict in (Verdict.WITHHOLD, Verdict.UNREADABLE):
                        boxes = [(0, 0, image.width, image.height)]
                        tally.withheld += 1
                    elif boxes:
                        tally.redacted += 1
                    else:
                        tally.passed += 1
                        stamped(f"PASSED    {path.name}, left untouched")
                        stamped(f"{result.decider_calls} local decisions, "
                                f"{result.decider_ms:.0f} ms | {tally.line()}")
                        print()
                        continue

                    redaction = redact(image, boxes, mode=mode)  # type: ignore[arg-type]
                    if replace:
                        redaction.image.save(path)
                        target = path.name
                    else:
                        target = f"{path.stem}.safe.png"
                        out = path.with_name(target)
                        redaction.image.save(out)
                        seen.add(out)

                    verb = "WITHHELD" if result.verdict is Verdict.WITHHOLD else "REDACTED"
                    stamped(f"{verb}  {redaction.count} region(s), "
                            f"{redaction.fraction_of(image):.0%} of the frame -> {target}")
                    if not replace:
                        stamped(f"NOTE the original {path.name} is UNCHANGED; "
                                f"attach {target} instead, or use --replace")
                    stamped(f"{result.decider_calls} local decisions, "
                            f"{result.decider_ms:.0f} ms | {tally.line()}")
                    print()
    except KeyboardInterrupt:
        pass

    print()
    print("=" * 72)
    print(f"  DISARMED -- {tally.line()}")
    if tally.secrets:
        print(f"  structured secrets caught: {', '.join(sorted(set(tally.secrets)))}")
    print(f"  {decider}")
    print("=" * 72)
    return 0
