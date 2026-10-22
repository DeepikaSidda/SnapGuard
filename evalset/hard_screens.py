"""Adversarial screens: the cases where a gate is most likely to be wrong.

The first six screens were too easy -- Decider, Nova 2 Lite and Nova Pro all scored 6/6,
which tells you nothing except that nobody failed. These four are built to provoke the
two failure modes that actually matter in use:

FALSE POSITIVES, which make the tool annoying enough to switch off:
  sha_soup       hex strings, UUIDs and git SHAs that look like secrets and are not
  public_metrics a dashboard full of money that belongs to nobody
  dark_editor    ordinary code on a very dark theme, low contrast, no secrets

FALSE NEGATIVES, the expensive kind:
  token_in_url   a credential hidden somewhere a glance would miss, in a URL bar

A gate that holds everything scores perfectly on the first six and is useless. These
screens are the ones that separate a usable rubric from a paranoid one.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from screens import W, H, chrome, font  # noqa: F401 - shared drawing helpers

HERE = Path(__file__).parent / "out"
HERE.mkdir(exist_ok=True)


def sha_soup() -> None:
    """Git output: long hex strings everywhere, not one of them a secret."""
    img = Image.new("RGB", (W, H), "#0f1014")
    d = ImageDraw.Draw(img)
    chrome(d, "bash - git log", dark=True)
    lines = [
        ("$ git log --format='%H %an %s' -n 12", "#c5c8c9"),
        ("", "#c5c8c9"),
        ("4f2a1c8e9b7d3056a1f7c3e9b5d8402a1c6f9e3b  Priya  fix pool sizing", "#d7ba7d"),
        ("9c3e7a1f5b8d2046e1a7f3c9b5d8401a2c6e9f3d  Tom    bump deps", "#d7ba7d"),
        ("1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d  Dana   add retry", "#d7ba7d"),
        ("e7f8091a2b3c4d5e6f708192a3b4c5d6e7f80912  Priya  revert 9c3e7a1", "#d7ba7d"),
        ("", "#c5c8c9"),
        ("$ docker images --digests | head -4", "#c5c8c9"),
        ("acme-payments  latest  sha256:3b8f1c9e7a2d5046b1f8c3e9", "#c5c8c9"),
        ("acme-gateway   v2.4    sha256:7d2a9f1c8b3e5047a2f9c4e1", "#c5c8c9"),
        ("", "#c5c8c9"),
        ("$ cat request-id.txt", "#c5c8c9"),
        ("x-amzn-requestid: 550e8400-e29b-41d4-a716-446655440000", "#c5c8c9"),
        ("x-amzn-trace-id:  Root=1-5e1b4151-5ac6c58f5b1b4151", "#c5c8c9"),
        ("", "#c5c8c9"),
        ("$ openssl dgst -sha256 build.tar.gz", "#c5c8c9"),
        ("SHA256(build.tar.gz)= a3f1c89e7b2d50461af7c3e9b5d8402a", "#c5c8c9"),
    ]
    for i, (text, colour) in enumerate(lines):
        d.text((32, 76 + i * 30), text, font=font(17, mono=True), fill=colour)
    img.save(HERE / "sha_soup.png")


def public_metrics() -> None:
    """A revenue dashboard. Full of money, none of it a person's account."""
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    chrome(d, "Public Metrics - Acme Status", dark=False)
    d.text((48, 72), "Platform metrics, Q3 (public)", font=font(30), fill="#14141c")
    d.text((48, 116), "Published quarterly. Aggregate figures only.",
           font=font(16), fill="#73737f")

    cards = [
        ("Gross volume processed", "$48.2M", "+12% QoQ"),
        ("Active merchants", "11,480", "+340"),
        ("Median settlement", "1.4 days", "-0.2"),
        ("Uptime", "99.98%", "SLA 99.9%"),
    ]
    for i, (label, value, delta) in enumerate(cards):
        x = 48 + (i % 2) * 920
        y = 170 + (i // 2) * 180
        d.rectangle([x, y, x + 860, y + 140], fill="#f7f7fa", outline="#dcdce4")
        d.text((x + 28, y + 24), label, font=font(17), fill="#73737f")
        d.text((x + 28, y + 56), value, font=font(38), fill="#14141c")
        d.text((x + 28, y + 106), delta, font=font(15), fill="#2f8f4f")

    d.text((48, 560), "Volume by month", font=font(22), fill="#14141c")
    for i, height in enumerate((120, 160, 150, 190, 210, 230)):
        x = 80 + i * 150
        d.rectangle([x, 820 - height, x + 90, 820], fill="#2f6fed")
        d.text((x + 20, 832), f"M{i + 1}", font=font(15), fill="#73737f")
    img.save(HERE / "public_metrics.png")


def dark_editor() -> None:
    """Ordinary source on a very dark theme. Low contrast, nothing secret."""
    img = Image.new("RGB", (W, H), "#0b0b0f")
    d = ImageDraw.Draw(img)
    chrome(d, "pool.py - acme-payments", dark=True)
    d.rectangle([0, 44, 280, H], fill="#08080b")
    for i, name in enumerate(("pool.py", "migrate.py", "models.py", "settings.py")):
        d.text((24, 74 + i * 30), name, font=font(15),
               fill="#e0e0e8" if i == 0 else "#4a4a56")

    code = [
        ("class ConnectionPool:", "#569cd6"),
        ("    def __init__(self, dsn, size=5):", "#dcdcaa"),
        ("        self._dsn = dsn", "#c8c8d0"),
        ("        self._size = size", "#c8c8d0"),
        ("        self._free = deque(maxlen=size)", "#c8c8d0"),
        ("        self._lock = Lock()", "#c8c8d0"),
        ("", "#c8c8d0"),
        ("    def acquire(self, timeout=5.0):", "#dcdcaa"),
        ("        deadline = monotonic() + timeout", "#c8c8d0"),
        ("        while monotonic() < deadline:", "#c586c0"),
        ("            with self._lock:", "#c586c0"),
        ("                if self._free:", "#c586c0"),
        ("                    return self._free.popleft()", "#c8c8d0"),
        ("            sleep(0.01)", "#c8c8d0"),
        ("        raise PoolTimeout(self._dsn)", "#c8c8d0"),
    ]
    for i, (text, colour) in enumerate(code):
        y = 90 + i * 36
        d.text((300, y), str(i + 1), font=font(15, mono=True), fill="#2e2e38")
        d.text((350, y), text, font=font(18, mono=True), fill=colour)
    img.save(HERE / "dark_editor.png")


def token_in_url() -> None:
    """A real credential, somewhere easy to miss: a browser address bar.

    The page itself is innocuous. The secret is one line of small text at the top, which
    is exactly the case the 448 px downscale is worst at and the reason the gate tiles.
    """
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    chrome(d, "Acme Admin - Reports", dark=False)

    d.rectangle([0, 44, W, 104], fill="#f3f3f7", outline="#dcdce4")
    d.rounded_rectangle([120, 56, W - 120, 92], 8, fill="white", outline="#c9c9d2")
    # Invented, and marked EXAMPLE so no scanner mistakes it for a live token. It still
    # matches the ghp_ shape, which is all the gate needs to see.
    d.text((140, 65),
           "https://admin.acme.dev/reports?export=csv&token=ghp_EXAMPLE9fK2mQ7xZ1aB4cD8eF0gH3jL5n",
           font=font(15, mono=True), fill="#3a3a46")

    d.text((48, 150), "Reports", font=font(34), fill="#14141c")
    d.text((48, 206), "Export your monthly reconciliation as CSV.",
           font=font(19), fill="#55555f")
    d.rounded_rectangle([48, 260, 268, 310], 6, fill="#2f6fed")
    d.text((92, 275), "Download CSV", font=font(16), fill="white")

    headers = ("Month", "Transactions", "Volume", "Status")
    for i, head in enumerate(headers):
        d.text((48 + i * 400, 380), head, font=font(16), fill="#73737f")
    rows = [("July", "182,443", "$14.2M", "Closed"),
            ("August", "191,208", "$15.8M", "Closed"),
            ("September", "204,771", "$18.2M", "Open")]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            d.text((48 + c * 400, 420 + r * 42), cell, font=font(17), fill="#30303c")
    img.save(HERE / "token_in_url.png")


if __name__ == "__main__":
    for fn in (sha_soup, public_metrics, dark_editor, token_in_url):
        fn()
        print(f"wrote {fn.__name__}.png")


# --------------------------------------------------------------------------- coverage
#
# Two rules shipped without a single test image behind them: identity_document and
# personal_contact. They were written from intuition and never verified, which is worse
# than not having them, because the README listed them as things the gate detects.
#
# A caveat that belongs in the results rather than hidden here: a *drawn* face is a weak
# proxy for a photograph. The rule asks about "a photograph of a person's face", and PIL
# shapes are not a photograph. If the rule fails on these, the honest reading is "not
# demonstrated either way" rather than "the rule is broken" -- and that is how it is
# reported.


def id_card() -> None:
    """A driving-licence style record on screen: portrait, name, date of birth, number."""
    img = Image.new("RGB", (W, H), "#eef0f4")
    d = ImageDraw.Draw(img)
    chrome(d, "Record 88241 - Membership Administration", dark=False)

    d.text((48, 72), "Member record", font=font(28), fill="#14141c")

    # The card.
    card = (60, 140, 980, 660)
    d.rounded_rectangle(card, 18, fill="#ffffff", outline="#b9bfcc", width=2)
    d.rounded_rectangle((60, 140, 980, 230), 18, fill="#1b4e8f")
    d.text((92, 168), "NATIONAL DRIVING LICENCE", font=font(24), fill="white")

    # Portrait: a face-like arrangement. Cartoonish by necessity -- see the note above.
    px, py = 110, 270
    d.rectangle([px, py, px + 230, py + 300], fill="#d7dae2", outline="#9aa1b1")
    d.ellipse([px + 55, py + 180, px + 175, py + 330], fill="#c98f6a")       # shoulders
    d.ellipse([px + 68, py + 48, px + 162, py + 186], fill="#e0a67c")        # head
    d.ellipse([px + 66, py + 44, px + 164, py + 96], fill="#3b2f2a")         # hair
    d.ellipse([px + 92, py + 108, px + 106, py + 120], fill="#2a2a30")       # left eye
    d.ellipse([px + 126, py + 108, px + 140, py + 120], fill="#2a2a30")      # right eye
    d.line([(px + 115, py + 122), (px + 115, py + 142)], fill="#b07a56", width=3)
    d.arc([px + 98, py + 136, px + 134, py + 162], 10, 170, fill="#8d5a46", width=4)

    fields = [
        ("SURNAME", "HARPER"),
        ("GIVEN NAMES", "JORDAN ALEX"),
        ("DATE OF BIRTH", "14.03.1991"),
        ("LICENCE NO.", "HARPE903149JA7NM"),
        ("ISSUED", "02.08.2023"),
        ("EXPIRES", "02.08.2033"),
    ]
    for i, (label, value) in enumerate(fields):
        y = 272 + i * 58
        d.text((380, y), label, font=font(13), fill="#73798a")
        d.text((380, y + 20), value, font=font(21), fill="#14141c")

    d.text((60, 700), "Verified against the national register on 19.10.2026",
           font=font(15), fill="#73798a")
    img.save(HERE / "id_card.png")


def contacts() -> None:
    """A CRM contact list: real-looking names, emails, phone numbers, home addresses."""
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    chrome(d, "Contacts - Northfield CRM", dark=False)

    d.rectangle([0, 44, W, 120], fill="#f6f7fa", outline="#e2e5ec")
    d.text((48, 70), "Contacts  -  412 records", font=font(24), fill="#14141c")

    headers = ("NAME", "EMAIL", "PHONE", "HOME ADDRESS")
    xs = (48, 430, 920, 1230)
    for x, head in zip(xs, headers):
        d.text((x, 150), head, font=font(14), fill="#73798a")
    d.line([(48, 178), (W - 48, 178)], fill="#e2e5ec", width=2)

    rows = [
        ("Priya Raman", "priya.raman@northfield.co", "+44 7700 900318",
         "14 Marlow Gardens, Leeds LS6 2QT"),
        ("Tomas Alvarez", "t.alvarez@gmail.com", "+1 (415) 555-0162",
         "882 Clayton St, San Francisco CA"),
        ("Dana Okafor", "dana.okafor@northfield.co", "+234 802 555 0147",
         "27 Awolowo Road, Ikoyi, Lagos"),
        ("Marta Kowalczyk", "marta.k91@outlook.com", "+48 601 555 284",
         "ul. Dluga 14/3, 31-147 Krakow"),
        ("Samuel Devlin", "sam.devlin@northfield.co", "+353 85 555 0119",
         "6 Grattan Court, Dublin 2"),
        ("Aisha Siddiqui", "aisha.s@fastmail.com", "+91 98200 55471",
         "Flat 702, Pali Hill, Mumbai 400050"),
        ("Henrik Bakke", "h.bakke@northfield.co", "+47 905 55 218",
         "Storgata 44B, 0184 Oslo"),
    ]
    for i, row in enumerate(rows):
        y = 200 + i * 78
        if i % 2 == 0:
            d.rectangle([48, y - 12, W - 48, y + 54], fill="#fafbfd")
        d.ellipse([52, y + 2, 84, y + 34], fill="#c6cbd8")
        for x, cell, size in zip(xs, row, (18, 17, 17, 16)):
            offset = 44 if x == 48 else 0
            d.text((x + offset, y + 8), cell, font=font(size), fill="#2c303a")

    img.save(HERE / "contacts.png")


def _extra() -> None:
    for fn in (id_card, contacts):
        fn()
        print(f"wrote {fn.__name__}.png")


if __name__ == "__main__" and "--extra" in __import__("sys").argv:
    _extra()
