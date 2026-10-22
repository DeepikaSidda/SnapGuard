"""A local quarantine for screenshots the gate refused to let through.

The problem this solves: the guard replaces your clipboard with a redacted copy, which is
the point -- but the original is then gone. If you wanted that screenshot for yourself,
for a bug report, for a colleague on the same network, for your own notes, you have lost
it to a tool that was only supposed to stop you *pasting* it.

So: the redacted copy goes to your clipboard, and the original goes here.

**This writes secrets to disk, and that is a real change in posture.** Everything else in
Aperture is deliberately ephemeral -- images live in memory, logs record verdicts and
numbers but never pixels. The vault is the one exception, so:

* It is **off by default**. You pass `--vault` to turn it on.
* It writes a README into the folder on first use, saying plainly what is in there.
* It best-effort restricts the folder to your user account, and tells you if it could
  not.
* Each capture gets a sidecar JSON with the verdict and the scores, so you know *why* a
  file is quarantined without opening it.
* Retention is finite by default. Secrets accumulating on disk forever is how this
  feature turns into the problem it was meant to solve.

Nothing in here is encrypted. A vault is a tidy drawer, not a safe. If the screenshot is
sensitive enough to need encryption, do not keep it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from PIL import Image

def default_directory() -> Path:
    """Where blocked captures go unless told otherwise.

    `APERTURE_VAULT` overrides it, so a path can be set once in the environment rather
    than typed on every invocation.

    The default deliberately sits under the home directory and not in Pictures,
    Documents or Desktop. On the machine this was built on, Pictures is redirected into
    OneDrive -- a quarantine full of unencrypted credentials inside a cloud-sync root
    would upload every blocked secret, which is precisely the thing this tool exists to
    prevent. `_warn_if_synced` catches that case wherever the folder ends up.
    """
    override = os.environ.get("APERTURE_VAULT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aperture" / "quarantine"


# Kept as a module attribute because callers import it, but resolve through
# `default_directory()` so the environment variable is honoured at call time rather
# than at import time.
DEFAULT_DIRECTORY = default_directory()

# Keep this many captures, newest first. Chosen to be useful for a working day without
# quietly becoming an archive of every secret you have ever had on screen.
DEFAULT_KEEP = 20
DEFAULT_MAX_AGE_DAYS = 7

README = """# Aperture quarantine

**Everything in this folder is a screenshot that was blocked from leaving your machine,
stored here in its ORIGINAL, UNREDACTED form.**

That means the files here may contain access keys, passwords, tokens, private
conversations, account balances, identity documents or other people's personal data.
They are the reason the gate fired.

They are stored in the clear. Nothing here is encrypted.

## Why this folder exists

When the guard blocks a screenshot it puts a redacted copy on your clipboard, so you
cannot accidentally paste the sensitive version. The original would otherwise be lost,
and sometimes you still want it -- for a bug report, for your own notes, to show a
colleague over a channel you trust.

## What you should do with it

- Treat it like a folder full of credentials, because that is what it is.
- Do not sync it. Check that it is not inside OneDrive, Dropbox, iCloud or a git
  repository.
- Empty it when you are done: `aperture vault --purge`

## Retention

Aperture keeps the newest {keep} captures and deletes anything older than
{days} days, each time it writes a new one. It will not grow without bound, but it is
your folder and it is your responsibility.

Each `.png` has a `.json` beside it recording the verdict and the scores that produced
it, so you can tell why something is here without opening the image.
"""


@dataclass
class Entry:
    """One quarantined capture."""

    image_path: Path
    meta_path: Path
    stored_at: datetime
    verdict: str
    reasons: list[str] = field(default_factory=list)
    size: tuple[int, int] = (0, 0)
    bytes_on_disk: int = 0

    @property
    def age(self) -> timedelta:
        return datetime.now() - self.stored_at

    def describe(self) -> str:
        reason = "; ".join(self.reasons) or "no reason recorded"
        age_hours = self.age.total_seconds() / 3600
        when = f"{age_hours:.0f}h ago" if age_hours < 48 else f"{age_hours / 24:.0f}d ago"
        return (
            f"{self.image_path.name}  {self.size[0]}x{self.size[1]}  "
            f"{self.bytes_on_disk / 1024:.0f} KB  {self.verdict.upper()}  "
            f"{when}  -  {reason}"
        )


class Vault:
    """Writes blocked captures to a local folder, with their verdicts beside them."""

    def __init__(
        self,
        directory: Path | str | None = None,
        *,
        keep: int = DEFAULT_KEEP,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        self.directory = Path(directory) if directory else default_directory()
        self.keep = keep
        self.max_age_days = max_age_days
        self.warnings: list[str] = []

    # ---- setup -------------------------------------------------------------

    def prepare(self) -> None:
        """Create the folder, write the README, and try to lock it down."""
        first_time = not self.directory.exists()
        self.directory.mkdir(parents=True, exist_ok=True)

        readme = self.directory / "README.md"
        if first_time or not readme.exists():
            readme.write_text(
                README.format(keep=self.keep, days=self.max_age_days), encoding="utf-8"
            )

        self._restrict()
        self._warn_if_synced()

    def _restrict(self) -> None:
        """Best effort: make the folder readable only by this user.

        Reported rather than assumed. A tool that silently fails to protect a folder
        full of credentials is worse than one that says it could not.
        """
        try:
            if sys.platform == "win32":
                # Break inheritance, then grant only the current user.
                user = os.environ.get("USERNAME")
                if not user:
                    self.warnings.append("could not determine the current user")
                    return
                for args in (
                    ["icacls", str(self.directory), "/inheritance:r"],
                    ["icacls", str(self.directory), "/grant:r", f"{user}:(OI)(CI)F"],
                ):
                    result = subprocess.run(
                        args, capture_output=True, text=True, timeout=20
                    )
                    if result.returncode != 0:
                        self.warnings.append(
                            f"could not restrict permissions ({result.stderr.strip()[:80]})"
                        )
                        return
            else:
                self.directory.chmod(0o700)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"could not restrict permissions ({exc})")

    def _warn_if_synced(self) -> None:
        """A quarantine inside a cloud-sync folder defeats the entire point."""
        parts = {p.lower() for p in self.directory.resolve().parts}
        for service in ("onedrive", "dropbox", "google drive", "icloud", "iclouddrive",
                        "box", "nextcloud", "syncthing"):
            if any(service in part for part in parts):
                self.warnings.append(
                    f"this folder looks like it is inside {service} -- blocked "
                    f"screenshots would be uploaded to the cloud, which defeats the "
                    f"point. Pass --vault with a different path."
                )
        if (self.directory / ".git").exists() or any(
            (p / ".git").exists() for p in self.directory.parents
        ):
            self.warnings.append(
                "this folder appears to be inside a git repository -- do not commit it"
            )

    # ---- writing -----------------------------------------------------------

    def store(
        self,
        image: Image.Image,
        *,
        verdict: str,
        reasons: Sequence[str] = (),
        scores: dict | None = None,
        secret_kinds: Sequence[str] = (),
        redacted: Image.Image | None = None,
    ) -> Path:
        """Save the original capture and a record of why it was blocked.

        Returns the path to the stored image.
        """
        self.prepare()

        # Milliseconds, not seconds. Two captures inside the same second tie on
        # `stored_at`, which made "newest first" arbitrary -- and taking two screenshots
        # in quick succession is completely normal.
        stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S.%f")[:-3]
        # The filename carries the verdict but never any content, because filenames end
        # up in logs, shells and screen recordings.
        base = f"{stamp}_{verdict}"
        image_path = self.directory / f"{base}.png"
        counter = 1
        while image_path.exists():
            image_path = self.directory / f"{base}_{counter}.png"
            counter += 1

        image.convert("RGB").save(image_path)

        if redacted is not None:
            redacted.convert("RGB").save(
                image_path.with_name(f"{image_path.stem}.redacted.png")
            )

        meta = {
            "stored_at": datetime.now().isoformat(timespec="milliseconds"),
            "verdict": verdict,
            "reasons": list(reasons),
            "secret_kinds": list(dict.fromkeys(secret_kinds)),
            "size": list(image.size),
            "region_scores": scores or {},
            "note": "ORIGINAL, UNREDACTED. May contain live credentials.",
        }
        image_path.with_suffix(".json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

        self.purge(keep=self.keep, older_than_days=self.max_age_days)
        return image_path

    # ---- reading and clearing ---------------------------------------------

    def entries(self) -> list[Entry]:
        """Everything in the vault, newest first."""
        if not self.directory.exists():
            return []
        out: list[Entry] = []
        for image_path in sorted(self.directory.glob("*.png")):
            if image_path.name.endswith(".redacted.png"):
                continue
            meta_path = image_path.with_suffix(".json")
            verdict, reasons, size = "unknown", [], (0, 0)
            stored = datetime.fromtimestamp(image_path.stat().st_mtime)
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    verdict = meta.get("verdict", verdict)
                    reasons = meta.get("reasons", [])
                    size = tuple(meta.get("size", size))  # type: ignore[assignment]
                    stored = datetime.fromisoformat(meta.get("stored_at", stored.isoformat()))
                except (ValueError, OSError):
                    pass
            out.append(
                Entry(
                    image_path=image_path,
                    meta_path=meta_path,
                    stored_at=stored,
                    verdict=verdict,
                    reasons=reasons,
                    size=size,  # type: ignore[arg-type]
                    bytes_on_disk=image_path.stat().st_size,
                )
            )
        # Filename as the tiebreaker, so ordering is deterministic even if two captures
        # share a timestamp or a sidecar is missing and we fall back to mtime.
        return sorted(out, key=lambda e: (e.stored_at, e.image_path.name), reverse=True)

    def purge(
        self, *, keep: int | None = None, older_than_days: int | None = None
    ) -> int:
        """Delete old captures. Returns how many were removed.

        Called automatically after each write. A quarantine that only ever grows is an
        archive of your credentials, which is the opposite of the intent.
        """
        removed = 0
        entries = self.entries()

        doomed: list[Entry] = []
        if keep is not None and len(entries) > keep:
            doomed.extend(entries[keep:])
        if older_than_days is not None:
            cutoff = timedelta(days=older_than_days)
            doomed.extend(e for e in entries if e.age > cutoff and e not in doomed)

        for entry in doomed:
            for path in (
                entry.image_path,
                entry.meta_path,
                entry.image_path.with_name(f"{entry.image_path.stem}.redacted.png"),
            ):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
            removed += 1
        return removed

    def empty(self) -> int:
        """Delete everything except the README."""
        return self.purge(keep=0)

    def total_bytes(self) -> int:
        return sum(e.bytes_on_disk for e in self.entries())

    def banner(self) -> str:
        count = len(self.entries())
        return (
            f"quarantine: {self.directory}  ({count} capture"
            f"{'' if count == 1 else 's'}, {self.total_bytes() / 1024:.0f} KB, "
            f"keeping newest {self.keep} for {self.max_age_days} days)"
        )
