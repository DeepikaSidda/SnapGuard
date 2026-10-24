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

import hashlib
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from PIL import Image

from .decider import Decider
from .notify import Notifier
from .patterns import Policy, RegionGate, Verdict
from .questions import CONTEXT_RULES, REGION_RULES, SCREEN_KIND
from .mesh import tighten_all
from .redact import hold_placeholder, redact, resolve_boxes
from .scanner import ocr_available, scan
from .vault import Vault


def _verdict_lines(result, secrets: list[str], covered: float | None) -> list[str]:
    """What the notification says. Numbers first -- they are the point.

    Deliberately shows the confidence, not just the conclusion. A user who can see
    "credential 0.90" learns to trust a 0.9 and question a 0.55, which is the whole
    reason for using a calibrated model instead of a classifier.
    """
    lines: list[str] = []
    for finding in result.findings[:3]:
        where = "whole screen" if finding.tile.index < 0 else f"region {finding.tile.index}"
        lines.append(f"{finding.rule.name} {finding.value:.2f} - {where}")
    for kind in dict.fromkeys(secrets):
        lines.append(f"{kind} found by pattern match")
    if covered is not None:
        lines.append(f"{covered:.0%} of the frame blacked out")
    return lines or ["nothing sensitive found"]

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
    """Exact identity for an image, to recognise what we wrote back.

    This was a 16x16 grayscale thumbnail, on the reasoning that it is cheap and good
    enough to tell one screenshot from another. It is not. Two captures of the same page
    a few pixels apart reduce to the same 16x16 thumbnail, so the guard treated a
    genuinely new screenshot as one it had already inspected -- and because the skip was
    silent, the tool simply stopped responding. A user reported exactly that.

    Hashing the real pixels costs about 5ms on a 1762x692 frame, against a poll interval
    measured in seconds, so the saving was never worth having.
    """
    digest = hashlib.blake2b(image.convert("RGB").tobytes(), digest_size=16).digest()
    return (image.size, digest)


Box = tuple[int, int, int, int]


def _fast_lane(
    image: "Image.Image",
    aws_lane: bool,
    ocr_on: bool,
    stamped,  # noqa: ANN001
    tally,  # noqa: ANN001
) -> tuple[list[Box], list[Box] | None, list[Box] | None]:
    """Locate secrets precisely, and report which words may stay visible.

    Two implementations of the same job, and the choice has consequences beyond
    detection.

        local   rapidocr + 13 regexes.  Free, nothing leaves the machine. Finds 1
                credential of 4 on the demo fixture and 0 of 1 on a real capture, and
                gives boxes per LINE.
        aws     Textract + Comprehend.  Finds 4 of 4 and 2 of 2, and gives boxes per
                WORD. Uploads the capture. ~$0.0024 and ~6s.

    The per-word part matters as much as the detection. `mesh.tighten_all` will leave a
    text line visible if it was read confidently and reads as ordinary language -- that
    is what keeps a heading like "Your team's API key" on screen while the key below it
    goes. rapidocr cannot read 9px interface text, so on a small capture it whitelists
    almost nothing and the mesh covers almost everything in a flagged region. Word boxes
    also let a label stay while a value on the *same line* is covered, which a line box
    cannot express at all.

    Returns the hint boxes, and the words to reveal -- or None, meaning "work it out from
    the local OCR". See research/README.md sections 15 and 17.
    """
    hints: list[Box] = []
    reveal_words: list[Box] | None = None
    text_words: list[Box] | None = None

    if aws_lane:
        from .aws_lane import AwsLaneUnavailable
        from .aws_lane import read as aws_read
        from .aws_lane import reveal_boxes as aws_reveal

        try:
            reading = aws_read(image)
        except AwsLaneUnavailable as exc:
            # Loudly, then fall through to the local lane. A recognition lane that fails
            # quietly would have the gate treat "the service was unreachable" as "there
            # are no secrets here".
            stamped(f"WARNING aws lane unavailable, using local OCR: {exc}")
        else:
            for secret in reading.secrets:
                stamped(f"aws    {secret.kind} ({secret.redacted_sample}) "
                        f"conf {secret.confidence:.2f} at {secret.box}")
                tally.secrets.append(secret.kind)
            hints = [s.box for s in reading.secrets]
            reveal_words = aws_reveal(reading)
            stamped(f"aws lane: {len(reading.secrets)} located, "
                    f"{len(reading.words)} words at {reading.upscale}x, "
                    f"{reading.total_ms:.0f} ms, ${reading.cost_usd:.4f}")
            text_words = [w.box for w in reading.words]
            return hints, reveal_words, text_words

    if ocr_on:
        started = time.perf_counter()
        secrets = scan(image)
        elapsed = (time.perf_counter() - started) * 1000
        if secrets:
            for secret in secrets:
                stamped(f"regex  {secret.kind} ({secret.redacted_sample}) "
                        f"at {secret.box}")
                tally.secrets.append(secret.kind)
            hints = [s.box for s in secrets]
        stamped(f"fast lane done in {elapsed:.0f} ms")

    return hints, reveal_words, text_words


def guard_clipboard(
    *,
    decider_url: str | None = None,
    minutes: int = 0,
    mesh: bool = True,
    reveal_labels: bool = True,
    aws_lane: bool = False,
    columns: int = 6,
    rows: int = 4,
    mode: str = "block",
    use_ocr: bool = True,
    notify: bool = True,
    vault: "Vault | None" = None,
    describe: bool = False,
) -> int:
    """Watch the clipboard until Ctrl+C, or until `minutes` have passed."""
    clipboard = get_clipboard()
    notifier = Notifier(enabled=notify)
    decider = Decider(decider_url)
    info = decider.require_vision()
    gate = RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        # Informational only, and it costs ~6s of the context pass. Off unless asked.
        info_questions=SCREEN_KIND if describe else None,
        policy=Policy(fail_closed=True),
        columns=columns,
        rows=rows,
    )

    ocr_on = use_ocr and ocr_available()
    deadline = time.time() + minutes * 60 if minutes else None
    tally = Tally()
    ignore: set[tuple] = set()
    last_seen: tuple | None = None

    print("=" * 72)
    print("  SNAPGUARD ARMED")
    print("=" * 72)
    print(f"  clipboard : {clipboard.name}")
    print(f"  decider   : {info.get('checkpoint')} on {info.get('device')}")
    print(f"  fast lane : {'OCR + regex' if ocr_on else 'off (no OCR installed)'}")
    print(f"  grid      : {columns}x{rows} regions, redaction mode '{mode}'")
    if deadline:
        print(f"  expires   : in {minutes} minutes")
    if vault:
        vault.prepare()
        print(f"  {vault.banner()}")
        for warning in vault.warnings:
            print(f"  WARNING   : {warning}")
    print()
    print("  images only -- text on the clipboard is never read")
    if vault:
        # The honest version of the line below, when the vault is on.
        print("  BLOCKED CAPTURES ARE SAVED UNREDACTED to the quarantine folder above")
    else:
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
            # Two different reasons to skip an image, which used to be one set and a
            # silent `continue`. They need separating, because conflating them meant
            # every image ever seen was skipped forever -- so taking the same screenshot
            # twice looked like the tool had died.
            #
            #   `ignore`     things WE put on the clipboard: the hold placeholder and
            #                the redacted result. Must never be re-inspected, for the
            #                life of the run.
            #   `last_seen`  the previous poll. The clipboard does not change between
            #                polls, so without this every frame is inspected repeatedly.
            #                Only the LAST one, so re-copying something is honoured.
            key = _fingerprint(image)
            if key in ignore:
                continue
            if key == last_seen:
                continue
            last_seen = key

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
                # Say so on screen immediately. The gate takes about a minute on a CPU,
                # and an unexplained minute of paste not working reads as a broken tool.
                notifier.hold(image.size)
            except Exception as exc:  # noqa: BLE001
                stamped(f"WARNING could not hold the clipboard: {exc}")

            # 3. Fast lane: exact, gives coordinates.
            hints, reveal_words, text_words = _fast_lane(
                image, aws_lane, ocr_on, stamped, tally
            )

            # 4. Judgement lane.
            result = gate.inspect(image, hint_boxes=hints)
            for name, value in result.context.items():
                if value >= 0.3:
                    stamped(f"decider {name}={value:.3f}")
            for finding in result.findings:
                stamped(f"decider {finding}")

            # 5. Put back whatever is safe, and say what happened.
            #
            # The scanner's exact boxes are added to the regions the model flagged, not
            # substituted for them: a regex hit proves there is *a* secret there, never
            # that it is the only one. Letting it shrink the region was tried and left
            # three credentials legible. See `redact.resolve_boxes`.
            boxes, confirmed = resolve_boxes(result.boxes(), hints)
            if confirmed:
                stamped(f"regex confirmed a secret inside {confirmed} flagged region(s)")
            found = [s for s in tally.secrets[-len(hints):]] if hints else []
            if result.verdict is Verdict.WITHHOLD or result.verdict is Verdict.UNREADABLE:
                tally.withheld += 1
                replacement = hold_placeholder(
                    image.size,
                    "withheld: " + (result.reasons[0] if result.reasons else "unsafe"),
                )
                clipboard.set_image(replacement)
                ignore.add(_fingerprint(replacement))
                stamped(f"WITHHELD  {chr(44).join(result.reasons)}")

                # The original is otherwise lost at this point: the clipboard now holds
                # a placeholder and nothing was ever written to disk. Quarantine it so
                # the user still has their screenshot, just not in a pasteable form.
                kept = None
                if vault:
                    kept = vault.store(
                        image,
                        verdict=result.verdict.value,
                        reasons=result.reasons,
                        scores=result.scores,
                        secret_kinds=found,
                    )
                    stamped(f"quarantined original -> {kept.name}")

                notifier.verdict(
                    kind="withhold",
                    title="Screenshot withheld",
                    lines=_verdict_lines(result, found, 1.0)
                    + (
                        [f"Original kept at {kept.name}"] if kept
                        else ["Your clipboard does NOT contain the original."]
                    ),
                    seconds=10,
                )
            elif boxes:
                tally.redacted += 1
                # The mesh. A flagged region keeps its verdict, but only the squares of
                # it that are not blank get painted -- so hiding one line of credentials
                # in an editor pane costs a bar, not the pane. Pixel arithmetic, no
                # model call, and it makes no guess about which text is sensitive, which
                # is what separates it from the regex-narrowing that was reverted.
                meshed, shrunk = tighten_all(image, boxes, enabled=mesh,
                                           reveal_labels=reveal_labels,
                                           reveal_boxes=reveal_words,
                                           text_boxes=text_words)
                if shrunk < 1.0:
                    stamped(f"mesh      kept {shrunk:.0%} of the flagged area, "
                            f"{len(boxes)} region(s) -> {len(meshed)} boxes")
                redaction = redact(image, meshed, mode=mode)  # type: ignore[arg-type]
                clipboard.set_image(redaction.image)
                ignore.add(_fingerprint(redaction.image))
                covered = redaction.fraction_of(image)
                stamped(f"REDACTED  {redaction.count} box(es), {covered:.0%} of the frame")

                kept = None
                if vault:
                    # Store the redacted copy alongside the original, so the folder is
                    # also a record of what the gate actually did.
                    kept = vault.store(
                        image,
                        verdict=result.verdict.value,
                        reasons=result.reasons,
                        scores=result.scores,
                        secret_kinds=found,
                        redacted=redaction.image,
                    )
                    stamped(f"quarantined original -> {kept.name}")

                notifier.verdict(
                    kind="redact",
                    title=f"Redacted {redaction.count} region"
                          f"{'s' if redaction.count != 1 else ''}",
                    lines=_verdict_lines(result, found, covered)
                    + ["Safe copy is on your clipboard."]
                    + ([f"Original kept at {kept.name}"] if kept else []),
                    seconds=9,
                )
            else:
                tally.passed += 1
                clipboard.set_image(image)
                stamped("PASSED    nothing found")
                notifier.verdict(
                    kind="pass",
                    title="Nothing sensitive found",
                    lines=[f"{image.width}x{image.height} checked locally",
                           "Original is back on your clipboard."],
                    seconds=4,
                )

            stamped(f"{result.decider_calls} local decisions, {result.decider_ms:.0f} ms | {tally.line()}")
            print()
    except KeyboardInterrupt:
        pass
    finally:
        # Never leave a "checking..." panel on screen after the guard has stopped.
        notifier.dismiss()

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
    mesh: bool = True,
    reveal_labels: bool = True,
    aws_lane: bool = False,
    columns: int = 6,
    rows: int = 4,
    mode: str = "block",
    use_ocr: bool = True,
    replace: bool = False,
    notify: bool = True,
    describe: bool = False,
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
        # Informational only, and it costs ~6s of the context pass. Off unless asked.
        info_questions=SCREEN_KIND if describe else None,
        policy=Policy(fail_closed=True),
        columns=columns,
        rows=rows,
    )
    ocr_on = use_ocr and ocr_available()
    deadline = time.time() + minutes * 60 if minutes else None
    tally = Tally()
    notifier = Notifier(enabled=notify)

    # Everything already present is ignored. The guard reacts to new captures; it does
    # not go rummaging through your existing pictures.
    seen: set[Path] = set()
    for directory in watched:
        seen.update(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        )

    print("=" * 72)
    print("  SNAPGUARD ARMED (folders)")
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

                    hints, reveal_words, text_words = _fast_lane(
                        image, aws_lane, ocr_on, stamped, tally
                    )

                    result = gate.inspect(image, hint_boxes=hints)
                    for finding in result.findings:
                        stamped(f"decider {finding}")

                    boxes, _confirmed = resolve_boxes(result.boxes(), hints)
                    if result.verdict in (Verdict.WITHHOLD, Verdict.UNREADABLE):
                        boxes = [(0, 0, image.width, image.height)]
                        tally.withheld += 1
                    elif boxes:
                        tally.redacted += 1
                        # Keep the regions, drop their blank parts. See mesh.py.
                        boxes, shrunk = tighten_all(
                            image, boxes, enabled=mesh,
                            reveal_labels=reveal_labels, reveal_boxes=reveal_words,
                            text_boxes=text_words,
                        )
                        if shrunk < 1.0:
                            stamped(f"mesh      kept {shrunk:.0%} of the flagged area")
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
                    covered = redaction.fraction_of(image)
                    stamped(f"{verb}  {redaction.count} box(es), "
                            f"{covered:.0%} of the frame -> {target}")
                    notifier.verdict(
                        kind="withhold" if verb == "WITHHELD" else "redact",
                        title=f"{path.name}: {verb.lower()}",
                        lines=_verdict_lines(result, [], covered)
                        + ([f"Original overwritten in place."] if replace
                           else [f"Safe copy written: {target}",
                                 "The original is UNCHANGED."]),
                        seconds=11,
                    )
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
