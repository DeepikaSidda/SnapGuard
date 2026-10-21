"""Draw realistic 1920x1080 mock screenshots with known ground truth.

Five screens, chosen so the feasibility test has both ends of the decision:

  ide_env         an editor with a .env file open, AWS key visible   -> must block
  bank            an online banking dashboard with balances          -> must block
  dm              a private one-to-one conversation                  -> must block
  terminal_error  a stack trace, nothing private                     -> must allow
  docs            public API documentation in a browser               -> must allow

Everything is drawn, so no real credential or person's data is involved and the
ground truth is exact. Keys are obviously fake.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent / "out"
HERE.mkdir(exist_ok=True)
W, H = 1920, 1080


def font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont:
    names = ("consola.ttf", "cour.ttf") if mono else ("segoeui.ttf", "arial.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def chrome(d: ImageDraw.ImageDraw, title: str, *, dark: bool) -> None:
    """A window title bar, so the screens read as application windows."""
    bar = "#2b2b31" if dark else "#e8e8ee"
    fg = "#d0d0d8" if dark else "#303038"
    d.rectangle([0, 0, W, 44], fill=bar)
    for i, colour in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        d.ellipse([20 + i * 26, 15, 34 + i * 26, 29], fill=colour)
    d.text((110, 12), title, font=font(17), fill=fg)


def ide_env() -> None:
    img = Image.new("RGB", (W, H), "#1e1e24")
    d = ImageDraw.Draw(img)
    chrome(d, "production.env - acme-payments - Visual Studio Code", dark=True)

    d.rectangle([0, 44, 300, H], fill="#191920")          # file tree
    for i, name in enumerate(
        ("src", "  handlers.py", "  models.py", "tests", ".env", "production.env",
         "README.md", "pyproject.toml")
    ):
        colour = "#ffffff" if name == "production.env" else "#8a8a99"
        d.text((24, 70 + i * 30), name, font=font(16), fill=colour)

    d.rectangle([300, 44, W, 80], fill="#23232b")
    d.text((320, 54), "production.env", font=font(15), fill="#dcdce4")

    lines = [
        ("1", "# production credentials - do not commit", "#6a9955"),
        ("2", "", "#d4d4d4"),
        ("3", "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "#ce9178"),
        ("4", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX", "#ce9178"),
        ("5", "DATABASE_URL=postgres://admin:Hunter2!@db.acme.internal:5432/payments", "#ce9178"),
        ("6", "STRIPE_SECRET_KEY=sk_test_REDACTEDFIXTURE", "#ce9178"),
        ("7", "JWT_SIGNING_SECRET=EXAMPLE9a1e4b8d2056f1a7c3e9b5d840", "#ce9178"),
        ("8", "", "#d4d4d4"),
        ("9", "SENTRY_DSN=https://a1b2c3@o456.ingest.sentry.io/789", "#ce9178"),
    ]
    for i, (num, text, colour) in enumerate(lines):
        y = 110 + i * 34
        d.text((330, y), num, font=font(16, mono=True), fill="#5a5a66")
        d.text((380, y), text, font=font(18, mono=True), fill=colour)

    img.save(HERE / "ide_env.png")


def bank() -> None:
    img = Image.new("RGB", (W, H), "#f6f7fa")
    d = ImageDraw.Draw(img)
    chrome(d, "Accounts - Northbridge Online Banking", dark=False)

    d.rectangle([0, 44, W, 130], fill="#0b4f9e")
    d.text((48, 72), "Northbridge Bank", font=font(28), fill="white")
    d.text((W - 300, 80), "Signed in: J. Harper", font=font(16), fill="#cfe0f5")

    d.text((48, 170), "Your accounts", font=font(30), fill="#14141c")
    rows = [
        ("Everyday Current  ****4417", "$2,841.19"),
        ("Savings Reserve   ****9032", "$48,205.76"),
        ("Platinum Credit   ****1188", "-$1,336.40"),
    ]
    for i, (label, amount) in enumerate(rows):
        y = 230 + i * 110
        d.rectangle([48, y, W - 48, y + 90], fill="white", outline="#d8d8e2")
        d.text((76, y + 20), label, font=font(20), fill="#30303c")
        d.text((76, y + 50), "Available balance", font=font(14), fill="#73737f")
        d.text((W - 300, y + 30), amount, font=font(26), fill="#14141c")

    d.text((48, 580), "Recent transactions", font=font(24), fill="#14141c")
    tx = [("19 Oct", "MERIDIAN HEALTH CLINIC", "-$340.00"),
          ("18 Oct", "PAYROLL - ACME CORP", "+$4,120.55"),
          ("17 Oct", "RENT - OAKWOOD PROPERTIES", "-$1,850.00")]
    for i, (date, who, amt) in enumerate(tx):
        y = 630 + i * 46
        d.text((76, y), date, font=font(17), fill="#73737f")
        d.text((200, y), who, font=font(17), fill="#30303c")
        d.text((W - 300, y), amt, font=font(17), fill="#30303c")

    img.save(HERE / "bank.png")


def dm() -> None:
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    chrome(d, "Priya Raman - Direct message", dark=False)

    d.rectangle([0, 44, 360, H], fill="#f3f3f7")
    d.text((28, 70), "Direct messages", font=font(18), fill="#6a6a78")
    for i, name in enumerate(("Priya Raman", "Tom Alvarez", "Design standup", "Dana Okafor")):
        colour = "#14141c" if i == 0 else "#55555f"
        d.ellipse([28, 112 + i * 56, 60, 144 + i * 56], fill="#c3c3d0")
        d.text((76, 120 + i * 56), name, font=font(17), fill=colour)

    d.text((400, 70), "Priya Raman", font=font(22), fill="#14141c")
    bubbles = [
        ("them", "please keep this between us for now"),
        ("them", "i'm going to hand in my notice on friday"),
        ("me", "oh wow. are you alright?"),
        ("them", "not really. the clinic results came back and"),
        ("them", "i need to take some proper time off"),
        ("me", "take whatever you need. i won't say anything"),
    ]
    y = 130
    for who, text in bubbles:
        wide = 11 * len(text) + 48
        if who == "them":
            d.rounded_rectangle([400, y, 400 + wide, y + 56], 14, fill="#ececf2")
            d.text((424, y + 17), text, font=font(18), fill="#1c1c24")
        else:
            d.rounded_rectangle([W - 60 - wide, y, W - 60, y + 56], 14, fill="#2f6fed")
            d.text((W - 36 - wide, y + 17), text, font=font(18), fill="white")
        y += 72

    img.save(HERE / "dm.png")


def terminal_error() -> None:
    img = Image.new("RGB", (W, H), "#0f1014")
    d = ImageDraw.Draw(img)
    chrome(d, "bash - pytest", dark=True)
    lines = [
        "$ pytest tests/test_invoice.py -q",
        "",
        "F                                                        [100%]",
        "=========================== FAILURES ===========================",
        "_____________________ test_total_with_discount _________________",
        "",
        "    def test_total_with_discount():",
        "        cart = Cart(items=[Item('widget', 1200, 2)])",
        "        cart.apply_discount('SPRING')",
        ">       assert cart.total() == 2040",
        "E       assert 2400 == 2040",
        "E        +  where 2400 = <Cart items=1>.total()",
        "",
        "tests/test_invoice.py:28: AssertionError",
        "",
        "TypeError: unsupported operand type(s) for *: 'NoneType' and 'int'",
        "  File 'billing/discount.py', line 54, in apply_discount",
        "    return base * self.rate",
        "",
        "1 failed in 0.42s",
    ]
    for i, text in enumerate(lines):
        colour = "#e05561" if text.startswith(("E ", "E  ", "TypeError", ">")) else "#c5c8c9"
        d.text((40, 70 + i * 30), text, font=font(18, mono=True), fill=colour)
    img.save(HERE / "terminal_error.png")


def docs() -> None:
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    chrome(d, "Pagination - Acme API Reference", dark=False)
    d.rectangle([0, 44, W, 100], fill="#fafafc", outline="#e4e4ec")
    d.text((40, 60), "docs.acme.dev/reference/pagination", font=font(16), fill="#6a6a78")

    d.rectangle([0, 100, 330, H], fill="#fafafc")
    for i, name in enumerate(("Getting started", "Authentication", "Pagination",
                              "Rate limits", "Webhooks", "Errors")):
        colour = "#2f6fed" if name == "Pagination" else "#55555f"
        d.text((36, 140 + i * 42), name, font=font(17), fill=colour)

    d.text((400, 150), "Pagination", font=font(38), fill="#14141c")
    body = [
        "All list endpoints return a page of at most 100 records. Use the",
        "cursor returned in `next_cursor` to request the following page.",
        "",
        "Requests omitting a cursor begin at the first page. A response",
        "with a null cursor is the last page of the collection.",
    ]
    for i, text in enumerate(body):
        d.text((400, 220 + i * 34), text, font=font(19), fill="#3a3a46")

    d.rectangle([400, 420, W - 80, 640], fill="#1e1e26")
    code = ["GET /v1/invoices?limit=50 HTTP/1.1", "Host: api.acme.dev", "",
            '{ "data": [ ... ], "next_cursor": "eyJpZCI6MTAwfQ" }']
    for i, text in enumerate(code):
        d.text((428, 448 + i * 34), text, font=font(18, mono=True), fill="#9cdcfe")
    img.save(HERE / "docs.png")


def deploy_failure() -> None:
    """The scenario the whole project is built around.

    One screen, two halves. The left half is a terminal with a deploy error -- exactly
    what you would want an assistant to read. The right half is an editor with a
    credentials file open, which must never leave the machine.

    This is the case that separates a useful gate from a blunt one: blocking the frame
    loses the error, sending the frame leaks the key, and the right answer is to send
    half of it.
    """
    img = Image.new("RGB", (W, H), "#1e1e24")
    d = ImageDraw.Draw(img)
    chrome(d, "acme-payments - Visual Studio Code", dark=True)

    midline = W // 2

    # ---- left: the terminal, with the error worth reading --------------------
    d.rectangle([0, 44, midline - 4, H], fill="#0f1014")
    d.text((28, 58), "TERMINAL", font=font(14), fill="#8a8a99")
    terminal_lines = [
        ("$ ./deploy.sh --env production", "#c5c8c9"),
        ("", "#c5c8c9"),
        ("Building image acme-payments:4f2a1c ... done", "#c5c8c9"),
        ("Pushing to registry ... done", "#c5c8c9"),
        ("Applying migrations ...", "#c5c8c9"),
        ("", "#c5c8c9"),
        ("Traceback (most recent call last):", "#e05561"),
        ('  File "migrate.py", line 61, in apply', "#c5c8c9"),
        ("    conn = pool.acquire(timeout=5)", "#c5c8c9"),
        ("  File \"pool.py\", line 118, in acquire", "#c5c8c9"),
        ("    raise PoolTimeout(self.dsn)", "#c5c8c9"),
        ("", "#c5c8c9"),
        ("PoolTimeout: could not acquire a connection", "#e05561"),
        ("  within 5.0s (pool size 5, in use 5)", "#e05561"),
        ("", "#c5c8c9"),
        ("deploy failed with exit code 1", "#e05561"),
    ]
    for i, (text, colour) in enumerate(terminal_lines):
        d.text((28, 92 + i * 30), text, font=font(17, mono=True), fill=colour)

    # ---- right: the editor, with the thing that must not leave ---------------
    d.rectangle([midline + 4, 44, W, H], fill="#1e1e24")
    d.rectangle([midline + 4, 44, W, 84], fill="#23232b")
    d.text((midline + 28, 55), "production.env", font=font(15), fill="#dcdce4")
    env_lines = [
        ("1", "# production - do not commit", "#6a9955"),
        ("2", "", "#d4d4d4"),
        ("3", "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "#ce9178"),
        ("4", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI", "#ce9178"),
        ("5", "  /K7MDENG/bPxRfiCYEXAMPLEKEY", "#ce9178"),
        ("6", "", "#d4d4d4"),
        ("7", "DATABASE_URL=postgres://admin:Hunter2!", "#ce9178"),
        ("8", "  @db.acme.internal:5432/payments", "#ce9178"),
        ("9", "", "#d4d4d4"),
        ("10", "STRIPE_SECRET_KEY=sk_live_51H8zQ2eZvKY", "#ce9178"),
        ("11", "DB_POOL_SIZE=5", "#b5cea8"),
    ]
    for i, (num, text, colour) in enumerate(env_lines):
        y = 110 + i * 34
        d.text((midline + 28, y), num, font=font(15, mono=True), fill="#5a5a66")
        d.text((midline + 72, y), text, font=font(16, mono=True), fill=colour)

    d.line([(midline, 44), (midline, H)], fill="#3a3a46", width=3)
    img.save(HERE / "deploy_failure.png")


if __name__ == "__main__":
    for fn in (ide_env, bank, dm, terminal_error, docs, deploy_failure):
        fn()
        print(f"wrote {fn.__name__}.png")
