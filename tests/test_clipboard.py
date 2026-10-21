"""Clipboard round-trip, because the write path is the easiest thing to get wrong.

Reading an image off the Windows clipboard is a one-liner through PIL. *Writing* one back
is not: there is no PIL API for it, so `WindowsClipboard.set_image` builds a BMP and
strips its 14-byte file header to produce the DIB that `CF_DIB` expects. Off-by-one there
and you either get a corrupted paste or a silent failure.

Every part of the guard depends on that write succeeding: the fail-closed hold, the
placeholder, and putting the redacted image back. So it gets a test.

This touches the real system clipboard, so it saves and restores whatever was there.

    .\\.venv\\Scripts\\python.exe -m pytest tests/test_clipboard.py -v
"""

from __future__ import annotations

import sys

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from aperture.redact import hold_placeholder, redact  # noqa: E402
from aperture.watch import _fingerprint, get_clipboard  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the tested backend is the Windows one"
)


@pytest.fixture
def clipboard():
    """Hand back the clipboard as we found it, image or not."""
    board = get_clipboard()
    original = board.get_image()
    yield board
    if original is not None:
        try:
            board.set_image(original)
        except Exception:  # noqa: BLE001 - best effort restore
            pass


def _distinct_image(size=(320, 200)) -> Image.Image:
    """Something with enough structure that a round-trip failure is obvious."""
    img = Image.new("RGB", size, (28, 32, 48))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, size[0] - 10, size[1] - 10], outline=(220, 64, 64), width=4)
    d.ellipse([40, 40, 140, 140], fill=(40, 200, 120))
    d.text((60, 160), "APERTURE ROUNDTRIP", fill=(240, 240, 250))
    return img


def test_write_then_read_preserves_the_image(clipboard):
    """The core claim: we can put an image back for the user to paste."""
    original = _distinct_image()
    clipboard.set_image(original)

    returned = clipboard.get_image()
    assert returned is not None, "nothing came back off the clipboard"
    assert returned.size == original.size

    # BMP is lossless, but the clipboard round-trip can drop the alpha channel and
    # reorder rows, so compare pixels rather than bytes.
    a = original.convert("RGB")
    b = returned.convert("RGB")
    diffs = sum(
        1
        for x in range(0, a.width, 7)
        for y in range(0, a.height, 7)
        if a.getpixel((x, y)) != b.getpixel((x, y))
    )
    sampled = len(range(0, a.width, 7)) * len(range(0, a.height, 7))
    assert diffs / sampled < 0.02, f"{diffs}/{sampled} sampled pixels differ"


def test_placeholder_can_be_written(clipboard):
    """The fail-closed hold. If this cannot be written, the guard has no teeth."""
    clipboard.set_image(hold_placeholder((600, 300), "checking this image"))
    held = clipboard.get_image()
    assert held is not None
    assert held.size == (600, 300)


def test_redacted_image_can_be_written(clipboard):
    """The end of the happy path: a redacted copy goes back for the user to paste."""
    original = _distinct_image((480, 300))
    result = redact(original, [(40, 40, 240, 140)], mode="block")
    clipboard.set_image(result.image)

    returned = clipboard.get_image()
    assert returned is not None
    assert returned.size == original.size
    # The redacted area must not match the original any more.
    assert returned.convert("RGB").getpixel((120, 90)) != original.getpixel((120, 90))


def test_fingerprint_distinguishes_images_and_survives_a_roundtrip(clipboard):
    """The guard uses fingerprints to avoid re-inspecting what it just wrote back.

    If a round-trip changed the fingerprint, the guard would loop: write redacted image,
    see 'new' image, inspect it again, forever.
    """
    a = _distinct_image()
    b = _distinct_image((320, 201))
    assert _fingerprint(a) != _fingerprint(b)

    clipboard.set_image(a)
    returned = clipboard.get_image()
    assert returned is not None
    assert _fingerprint(returned) == _fingerprint(a), (
        "a clipboard round-trip changed the fingerprint; the guard would re-inspect "
        "its own output in a loop"
    )
