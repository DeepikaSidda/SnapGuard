"""The quarantine folder: what it stores, and that it does not grow without bound.

No model and no network needed, so this is a real unit test rather than a smoke test.

The properties worth pinning are the ones a careless change would break quietly:
the original must stay unredacted (that is the whole feature), the redacted copy must be
distinguishable from it, retention must actually delete things, and the warnings about
cloud-synced paths must fire -- a quarantine inside OneDrive uploads every blocked
secret, which is the exact opposite of the intent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aperture.vault import Vault  # noqa: E402


def _image(tag: str = "SECRET", size=(300, 120)) -> Image.Image:
    img = Image.new("RGB", size, (20, 20, 26))
    ImageDraw.Draw(img).text((12, 50), tag, fill=(240, 240, 240))
    return img


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault(tmp_path / "quarantine", keep=3, max_age_days=7)


def test_prepare_writes_a_warning_readme(vault: Vault):
    vault.prepare()
    readme = vault.directory / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8").lower()
    # The README has to say what is in there. Someone will find this folder without
    # context, and it must be obvious immediately.
    assert "unredacted" in text
    assert "not encrypted" in text or "nothing here is encrypted" in text
    assert "purge" in text


def test_store_keeps_the_original_and_records_why(vault: Vault):
    original = _image("AKIA-LOOKING-THING")
    path = vault.store(
        original,
        verdict="redact",
        reasons=["a credential is visible"],
        scores={1: {"credential": 0.88}},
        secret_kinds=["aws_access_key_id", "aws_access_key_id"],
    )

    assert path.exists()
    # Byte-for-byte the same pixels: this is the point of the feature.
    assert list(Image.open(path).convert("RGB").getdata()) == list(
        original.convert("RGB").getdata()
    )

    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["verdict"] == "redact"
    assert meta["reasons"] == ["a credential is visible"]
    # Duplicates collapsed, so the record is readable.
    assert meta["secret_kinds"] == ["aws_access_key_id"]
    assert meta["size"] == [300, 120]
    assert "UNREDACTED" in meta["note"]


def test_filename_carries_the_verdict_but_never_content(vault: Vault):
    path = vault.store(_image("hunter2"), verdict="withhold", reasons=["private chat"])
    # Filenames end up in logs, shells and screen recordings.
    assert "withhold" in path.name
    assert "hunter2" not in path.name.lower()
    assert "private" not in path.name.lower()


def test_redacted_copy_is_stored_beside_the_original(vault: Vault):
    original = _image("SECRET")
    redacted = _image("[[[[[[]]]]]]")
    path = vault.store(original, verdict="redact", redacted=redacted)

    copy = path.with_name(f"{path.stem}.redacted.png")
    assert copy.exists()
    assert list(Image.open(copy).convert("RGB").getdata()) != list(
        Image.open(path).convert("RGB").getdata()
    ), "the redacted copy should not be identical to the original"


def test_retention_deletes_the_oldest(vault: Vault):
    for i in range(6):
        vault.store(_image(f"capture {i}"), verdict="redact")

    entries = vault.entries()
    assert len(entries) == 3, "keep=3 should have been enforced on write"
    # And the sidecars went with them, rather than being orphaned.
    assert len(list(vault.directory.glob("*.json"))) == 3


def test_entries_are_newest_first_and_describable(vault: Vault):
    vault.store(_image("one"), verdict="redact", reasons=["first"])
    vault.store(_image("two"), verdict="withhold", reasons=["second"])
    entries = vault.entries()
    assert entries[0].stored_at >= entries[1].stored_at
    assert "second" in entries[0].describe()
    assert "WITHHOLD" in entries[0].describe()


def test_empty_removes_captures_but_leaves_the_readme(vault: Vault):
    vault.store(_image(), verdict="redact")
    vault.store(_image(), verdict="withhold")
    assert vault.empty() == 2
    assert vault.entries() == []
    assert (vault.directory / "README.md").exists()


def test_a_cloud_synced_path_is_called_out(tmp_path: Path):
    """A quarantine inside OneDrive uploads every blocked secret."""
    synced = Vault(tmp_path / "OneDrive" / "Pictures" / "quarantine")
    synced.prepare()
    assert any("onedrive" in w.lower() for w in synced.warnings), synced.warnings


def test_a_git_repository_is_called_out(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    inside = Vault(tmp_path / "quarantine")
    inside.prepare()
    assert any("git" in w.lower() for w in inside.warnings), inside.warnings


def test_a_plain_path_produces_no_warnings(tmp_path: Path):
    """The warnings have to be meaningful, so they must not fire on a normal path."""
    clean = Vault(tmp_path / "somewhere" / "quarantine")
    clean.prepare()
    assert [w for w in clean.warnings if "onedrive" in w.lower() or "git" in w.lower()] == []
