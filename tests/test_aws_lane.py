"""The cloud lane's arithmetic, without calling the cloud.

`aws_lane.read` makes two network calls and everything interesting happens either side of
them: turning Textract blocks into words with pixel boxes and character offsets, then
turning a character span from Comprehend back into a box. Those are the parts that can be
silently wrong -- a geometry mistake puts the redaction in the wrong place, and an offset
mistake puts it on the wrong word -- and neither needs AWS to test.

So the service replies are synthesised here. That is not a shortcut: a test that called
the real services would cost money, need credentials, and still not pin the coordinate
maths, because it could not assert where the boxes ought to be.

The one property worth stating up front: `Secret.redacted_sample` must never contain the
secret. This module handles verbatim credential text from two sources, and the whole
point of masking is that a log of a redaction is not itself a leak.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from snapguard import aws_lane  # noqa: E402
from snapguard.aws_lane import (  # noqa: E402
    MIN_TEXT_PX,
    _assemble,
    _boxes_for_span,
    _dedupe,
    _entity_hits,
    _regex_hits,
    _scale_for,
    estimate_text_height,
    reveal_boxes,
)

SENT = (1000, 500)  # the size of the image handed to Textract


def word(text: str, left: float, top: float, width: float, confidence: float = 99.0):
    """A WORD block with normalised geometry, the way Textract returns it."""
    return {
        "BlockType": "WORD",
        "Id": f"w-{text}-{left}",
        "Text": text,
        "Confidence": confidence,
        "Geometry": {
            "BoundingBox": {"Left": left, "Top": top, "Width": width, "Height": 0.04}
        },
    }


def line(*words):
    return {
        "BlockType": "LINE",
        "Id": "l-" + "-".join(w["Text"] for w in words),
        "Geometry": {
            "BoundingBox": {"Left": 0.0, "Top": 0.0, "Width": 1.0, "Height": 0.04}
        },
        "Relationships": [{"Type": "CHILD", "Ids": [w["Id"] for w in words]}],
    }


def reply(*lines_of_words):
    blocks = []
    for words in lines_of_words:
        blocks.append(line(*words))
        blocks.extend(words)
    return {"Blocks": blocks}


# ---- geometry ----------------------------------------------------------------


def test_normalised_geometry_becomes_pixels():
    words, _ = _assemble(reply([word("hello", 0.1, 0.2, 0.3)]), SENT, upscale=1.0)
    left, top, right, bottom = words[0].box
    assert (left, top) == (100, 100)
    assert right == 100 + 300
    assert bottom == 100 + 20


def test_boxes_are_mapped_back_through_the_upscale():
    """A small capture is enlarged before sending, so boxes must be divided back down.

    Textract ignores text under 15px, so `read` scales the image up. If the geometry were
    not scaled back, every box would land at the wrong place on the original -- further
    out the further right it sits, which is the kind of bug that looks like "the
    redaction is slightly off" rather than "the maths is wrong".
    """
    plain, _ = _assemble(reply([word("x", 0.5, 0.5, 0.1)]), SENT, upscale=1.0)
    scaled, _ = _assemble(reply([word("x", 0.5, 0.5, 0.1)]), SENT, upscale=2.0)
    assert scaled[0].box == tuple(v // 2 for v in plain[0].box)


# ---- offsets -----------------------------------------------------------------


def test_offsets_line_up_with_the_assembled_text():
    words, text = _assemble(
        reply([word("alpha", 0.0, 0.0, 0.1), word("beta", 0.2, 0.0, 0.1)]),
        SENT, upscale=1.0,
    )
    for w in words:
        assert text[w.start:w.end] == w.text


def test_a_repeated_word_keeps_distinct_offsets():
    """The reason offsets are tracked while the text is built, not searched for after.

    `text.find(word)` would return the first occurrence every time, so a span pointing at
    the second `key` would be mapped onto the box of the first.
    """
    words, text = _assemble(
        reply([
            word("key", 0.0, 0.0, 0.1),
            word("and", 0.2, 0.0, 0.1),
            word("key", 0.4, 0.0, 0.1),
        ]),
        SENT, upscale=1.0,
    )
    first, _, second = words
    assert first.start != second.start
    assert text[second.start:second.end] == "key"
    assert first.box != second.box


def test_a_span_maps_to_every_word_it_touches():
    words, text = _assemble(
        reply([word("abc", 0.0, 0.0, 0.1), word("def", 0.2, 0.0, 0.1),
               word("ghi", 0.4, 0.0, 0.1)]),
        SENT, upscale=1.0,
    )
    # "abc def" -> the first two words, not the third.
    boxes = _boxes_for_span(words, 0, 7)
    assert len(boxes) == 2
    assert boxes == [words[0].box, words[1].box]


def test_lines_are_separated_in_the_text():
    _, text = _assemble(
        reply([word("one", 0.0, 0.0, 0.1)], [word("two", 0.0, 0.1, 0.1)]),
        SENT, upscale=1.0,
    )
    assert text == "one\ntwo\n"


# ---- the regex lane, on text it can finally read -----------------------------


def test_a_key_is_found_and_the_box_lands_on_the_value():
    """The finding that prompted this module: the patterns work, given readable text."""
    words, text = _assemble(
        reply([
            word("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                 0.1, 0.3, 0.5),
        ]),
        SENT, upscale=1.0,
    )
    hits = _regex_hits(words, text)
    assert hits, "the pattern should match once Textract has read the token"
    assert any("aws_secret" in h.kind for h in hits)


def test_the_sample_never_contains_the_secret():
    """A log of a redaction must not be the leak."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    words, text = _assemble(
        reply([word(secret, 0.1, 0.3, 0.5)]), SENT, upscale=1.0
    )
    hits = _regex_hits(words, text)
    assert hits
    for hit in hits:
        assert secret not in hit.redacted_sample
        assert "20 chars" in hit.redacted_sample


# ---- the Comprehend lane -----------------------------------------------------


def entity(kind: str, start: int, end: int, score: float):
    return {"Type": kind, "BeginOffset": start, "EndOffset": end, "Score": score}


def _one_line():
    return _assemble(
        reply([word("dcd_live_EXAMPLEa1B2c3D4e5F6g7H8i9J0kL1m", 0.1, 0.3, 0.5)]),
        SENT, upscale=1.0,
    )


def test_a_named_credential_becomes_a_box():
    words, text = _one_line()
    hits = _entity_hits(words, text, [entity("AWS_SECRET_KEY", 0, 39, 0.96)], 0.80)
    assert len(hits) == 1
    assert hits[0].kind == "credential:aws_secret_key"
    assert hits[0].confidence == pytest.approx(0.96)


def test_a_low_confidence_entity_is_dropped():
    """Measured: 0.96 on a real credential, 0.67 on an ADDRESS it invented."""
    words, text = _one_line()
    assert not _entity_hits(words, text, [entity("ADDRESS", 0, 39, 0.67)], 0.80)


def test_contact_entities_need_a_higher_score():
    """The chatty types fire on ordinary interface text, so their floor is higher."""
    words, text = _one_line()
    assert not _entity_hits(words, text, [entity("ADDRESS", 0, 39, 0.85)], 0.80)
    assert _entity_hits(words, text, [entity("ADDRESS", 0, 39, 0.95)], 0.80)


def test_uninteresting_types_are_ignored():
    """DATE_TIME fires on every log timestamp; covering the clock is over-covering."""
    words, text = _one_line()
    assert not _entity_hits(words, text, [entity("DATE_TIME", 0, 39, 1.0)], 0.80)


def test_a_very_short_span_is_ignored():
    """Measured: a lone '5' -- the port in :5432/payments -- came back as a key at 1.00."""
    words, text = _one_line()
    assert not _entity_hits(words, text, [entity("AWS_SECRET_KEY", 0, 1, 1.0)], 0.80)


def test_entity_samples_are_masked_too():
    words, text = _one_line()
    hits = _entity_hits(words, text, [entity("AWS_SECRET_KEY", 0, 39, 0.96)], 0.80)
    assert text[:39] not in hits[0].redacted_sample


# ---- dedupe ------------------------------------------------------------------


def test_the_same_secret_found_twice_is_one_finding():
    """A key matched by both the regex and Comprehend is one secret, not two."""
    words, text = _one_line()
    both = _regex_hits(words, text) + _entity_hits(
        words, text, [entity("AWS_SECRET_KEY", 0, 39, 0.96)], 0.80
    )
    assert len(_dedupe(both)) < len(both) or len(both) == 1


def test_distant_findings_are_kept_apart():
    words, text = _assemble(
        reply([word("AKIAIOSFODNN7EXAMPLE", 0.1, 0.1, 0.3)],
              [word("AKIAIOSFODNN7EXAMPLF", 0.1, 0.8, 0.3)]),
        SENT, upscale=1.0,
    )
    assert len(_dedupe(_regex_hits(words, text))) == 2


# ---- upscaling ---------------------------------------------------------------


def test_small_captures_are_scaled_past_the_service_floor():
    """Textract will not see text under 15px, and a 472px-tall capture has ~6px text."""
    small = Image.new("RGB", (1024, 472))
    height = estimate_text_height(small)
    assert height < MIN_TEXT_PX
    scale = _scale_for(small, height)
    assert scale > 1.0
    assert height * scale >= MIN_TEXT_PX


def test_even_a_1080p_capture_is_upscaled_a_little():
    """Because 14px UI text is below the service's 15px floor, narrowly.

    This was written expecting 1.0x and the assertion failed at 1.5x, which turned out
    to be correct: a 1080p screen's interface text really is under the limit. Worth
    pinning rather than fixing, because it means essentially every desktop capture gets
    enlarged before it is sent, and the no-upscale path below only applies from 4K up.
    """
    big = Image.new("RGB", (1920, 1080))
    assert estimate_text_height(big) < MIN_TEXT_PX
    assert _scale_for(big, estimate_text_height(big)) == 1.5


def test_a_4k_capture_is_sent_as_is():
    """Text on a 4K screen clears the floor on its own, so nothing is paid to enlarge it."""
    huge = Image.new("RGB", (3840, 2160))
    assert estimate_text_height(huge) >= MIN_TEXT_PX
    assert _scale_for(huge, estimate_text_height(huge)) == 1.0


def test_upscaling_is_bounded():
    """A thumbnail must not be enlarged without limit; the payload has to be sent."""
    tiny = Image.new("RGB", (64, 32))
    assert _scale_for(tiny, estimate_text_height(tiny)) <= 4.0


# ---- the reveal whitelist ----------------------------------------------------


def test_reveal_keeps_confident_prose_and_drops_the_key():
    blocks = reply([
        word("Your", 0.0, 0.3, 0.05, confidence=99.8),
        word("API", 0.06, 0.3, 0.04, confidence=99.5),
        word("key", 0.11, 0.3, 0.04, confidence=99.9),
        word("dcd_live_EXAMPLEa1B2c3D4e5F6g7H8i9J0kL1m", 0.2, 0.3, 0.5,
             confidence=55.1),
    ])
    words, text = _assemble(blocks, SENT, upscale=1.0)
    reading = aws_lane.Reading(
        words=words, text=text, secrets=[], textract_ms=0.0, comprehend_ms=0.0,
        cost_usd=0.0, upscale=1.0,
    )

    kept = reveal_boxes(reading)
    assert len(kept) == 3, "the three ordinary words should be revealed"
    assert words[3].box not in kept, "the key must not be revealed"


def test_a_word_read_with_low_confidence_is_never_revealed():
    """Measured: ordinary words came back above 99%, the credential at 55%."""
    blocks = reply([word("allowance", 0.0, 0.3, 0.1, confidence=40.0)])
    words, text = _assemble(blocks, SENT, upscale=1.0)
    reading = aws_lane.Reading(
        words=words, text=text, secrets=[], textract_ms=0.0, comprehend_ms=0.0,
        cost_usd=0.0, upscale=1.0,
    )
    assert reveal_boxes(reading) == []


# ---- failure behaviour -------------------------------------------------------


def test_blocks_without_children_are_skipped():
    """A LINE with no WORD children must not crash the assembly."""
    orphan = {
        "BlockType": "LINE",
        "Id": "lonely",
        "Geometry": {"BoundingBox": {"Left": 0, "Top": 0, "Width": 1, "Height": 0.1}},
    }
    words, text = _assemble({"Blocks": [orphan]}, SENT, upscale=1.0)
    assert words == []
    assert text == ""


def test_an_empty_reply_is_not_an_error():
    words, text = _assemble({"Blocks": []}, SENT, upscale=1.0)
    assert (words, text) == ([], "")
