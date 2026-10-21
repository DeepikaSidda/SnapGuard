"""Aperture's command line.

Four commands, and none of them run unless you run them. There is deliberately no
installer, no service, no autostart flag: a tool that inspects your screen should exist
only while you are looking at it.

    aperture check  <image>        inspect one image and print the verdict
    aperture ask    "<question>"   the agent: look at a screen, answer, gate the pixels
    aperture guard                 watch the clipboard for screenshots
    aperture doctor                check the decider server and Bedrock access
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

from .decider import Decider, DeciderUnavailable, VisionNotEnabled
from .patterns import Policy, RegionGate, Verdict
from .questions import CONTEXT_RULES, REGION_RULES, SCREEN_KIND
from .redact import redact
from .scanner import ocr_available, scan

BAR = "-" * 72


def _gate(decider: Decider, columns: int, rows: int) -> RegionGate:
    return RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        info_questions=SCREEN_KIND,
        policy=Policy(fail_closed=True),
        columns=columns,
        rows=rows,
    )


def _print_result(result, image: Image.Image) -> None:
    kind = result.info.get("screen_kind")
    if kind:
        print(f"  screen looks like   : {kind} "
              f"(confidence {result.info.get('screen_kind_confidence')})")
    for name, value in result.context.items():
        marker = " <-- fired" if value >= 0.55 else ""
        print(f"  {name:20s}: {value:.3f}{marker}")
    if result.findings:
        print("\n  findings:")
        for finding in result.findings:
            print(f"    {finding}")
    total = len(result.cleared) + len(result.flagged)
    if total:
        sending = len(result.injectable)
        unsafe = len(result.flagged)
        skipped = total - unsafe - sending
        print(f"\n  regions: {sending} of {total} selected to send")
        print(f"           {unsafe} withheld for safety, "
              f"{skipped} skipped as unrelated or unreadable")
        print(f"           {1 - sending / total:.0%} of the frame stays on this machine")
    print(f"\n  VERDICT: {result.verdict.value.upper()}")
    for reason in result.reasons:
        print(f"           {reason}")
    print(f"  cost   : {result.decider_calls} local decisions, "
          f"{result.decider_ms:.0f} ms, $0.00")


# --------------------------------------------------------------------------- check


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.image)
    if not path.exists():
        print(f"no such file: {path}")
        return 1
    image = Image.open(path)

    decider = Decider(args.decider_url)
    print(decider.banner())
    print(f"\n{BAR}\n{path.name}  {image.width}x{image.height}\n{BAR}")

    # The fast lane first. Exact, instant, and it gives the gate somewhere to look.
    hints: list[tuple[int, int, int, int]] = []
    if args.ocr and ocr_available():
        secrets = scan(image)
        print(f"  fast lane (OCR+regex): {len(secrets)} structured secret(s)")
        for secret in secrets:
            print(f"    {secret.kind}: {secret.redacted_sample} at {secret.box}")
        hints = [s.box for s in secrets]
    elif args.ocr:
        print("  fast lane: OCR not installed (pip install 'aperture-gate[ocr]')")

    result = _gate(decider, args.columns, args.rows).inspect(image, hint_boxes=hints)
    _print_result(result, image)

    if args.out and result.verdict in (Verdict.REDACT, Verdict.WITHHOLD):
        boxes = result.boxes() + hints
        if result.verdict is Verdict.WITHHOLD:
            boxes = [(0, 0, image.width, image.height)]
        redaction = redact(image, boxes, mode=args.mode)
        redaction.image.save(args.out)
        print(f"\n  wrote redacted copy to {args.out} "
              f"({redaction.fraction_of(image):.0%} of the frame covered)")
    return 0


# --------------------------------------------------------------------------- ask


def cmd_ask(args: argparse.Namespace) -> int:
    try:
        from .agent import build
    except ImportError as exc:
        print(f"the agent needs extra packages: {exc}")
        print("install with: pip install 'aperture-gate[agent]'")
        return 1

    screen = Path(args.screen) if args.screen else None
    if screen and not screen.exists():
        print(f"no such file: {screen}")
        return 1

    def announce(result) -> None:
        print(f"\n{BAR}\n  THE GATE RAN\n{BAR}")
        _print_result(result, Image.new("RGB", (1, 1)))
        print(BAR)

    session = build(
        screen=screen,
        decider_url=args.decider_url,
        columns=args.columns,
        rows=args.rows,
        on_decision=announce,
    )
    print(session.decider.banner())
    print(f"agent  : {args.model or 'us.amazon.nova-2-lite-v1:0'} on Amazon Bedrock")
    print(f"screen : {screen or 'live capture'}")
    print(f"\nUSER: {args.question}\n")

    reply = session(args.question)

    print(f"\n{BAR}\n  WHAT THE AGENT SAID\n{BAR}")
    print(reply)
    print(f"\n{BAR}\n{session.report()}\n{BAR}")
    for row in session.gate.log:
        print(f"  {row['verdict']:9s} requested {row['requested']} "
              f"-> allowed {row['allowed']}, withheld {row['withheld']}")
    return 0


# --------------------------------------------------------------------------- guard


def cmd_guard(args: argparse.Namespace) -> int:
    from .watch import guard_clipboard, guard_folders

    if args.folders:
        return guard_folders(
            decider_url=args.decider_url,
            minutes=args.for_minutes,
            columns=args.columns,
            rows=args.rows,
            mode=args.mode,
            use_ocr=args.ocr,
            replace=args.replace,
        )
    return guard_clipboard(
        decider_url=args.decider_url,
        minutes=args.for_minutes,
        columns=args.columns,
        rows=args.rows,
        mode=args.mode,
        use_ocr=args.ocr,
    )


# --------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True

    print("decider server")
    try:
        decider = Decider(args.decider_url)
        info = decider.require_vision()
        print(f"  ok    {info.get('checkpoint')} on {info.get('device')}, vision on")
    except (DeciderUnavailable, VisionNotEnabled) as exc:
        print(f"  FAIL  {exc}")
        ok = False

    print("\nOCR fast lane")
    print("  ok    installed" if ocr_available()
          else "  warn  not installed; pip install 'aperture-gate[ocr]'")

    print("\nclipboard")
    try:
        from .watch import clipboard_backend

        print(f"  ok    {clipboard_backend()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  warn  unavailable: {exc}")

    print("\nAmazon Bedrock")
    try:
        import boto3

        client = boto3.client("bedrock-runtime", region_name="us-east-1")
        client.converse(
            modelId="us.amazon.nova-2-lite-v1:0",
            messages=[{"role": "user", "content": [{"text": "ok"}]}],
            inferenceConfig={"maxTokens": 5},
        )
        print("  ok    us.amazon.nova-2-lite-v1:0 reachable")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {str(exc)[:160]}")
        ok = False

    return 0 if ok else 1


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aperture",
        description="Decide locally how much of your screen may reach a cloud model.",
    )
    parser.add_argument("--decider-url", default=None,
                        help="default http://127.0.0.1:8099")
    parser.add_argument("--columns", type=int, default=2, help="region grid columns")
    parser.add_argument("--rows", type=int, default=2, help="region grid rows")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="inspect one image")
    check.add_argument("image")
    check.add_argument("--out", help="write a redacted copy here")
    check.add_argument("--mode", choices=["block", "blur"], default="block")
    check.add_argument("--no-ocr", dest="ocr", action="store_false")
    check.set_defaults(func=cmd_check, ocr=True)

    ask = sub.add_parser("ask", help="run the agent against a screen")
    ask.add_argument("question")
    ask.add_argument("--screen", help="an image to use instead of a live capture")
    ask.add_argument("--model", default=None)
    ask.set_defaults(func=cmd_ask)

    guard = sub.add_parser("guard", help="watch the clipboard, or screenshot folders")
    guard.add_argument("--for", dest="for_minutes", type=int, default=0,
                       help="stop after N minutes (0 = until Ctrl+C)")
    guard.add_argument("--mode", choices=["block", "blur"], default="block")
    guard.add_argument("--no-ocr", dest="ocr", action="store_false")
    guard.add_argument("--folders", action="store_true",
                       help="watch screenshot folders instead of the clipboard "
                            "(Win+PrtScn and Save-as write files, not clipboard images)")
    guard.add_argument("--replace", action="store_true",
                       help="with --folders, overwrite the original instead of writing "
                            "a .safe.png copy beside it")
    guard.set_defaults(func=cmd_guard, ocr=True)

    doctor = sub.add_parser("doctor", help="check the moving parts")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (DeciderUnavailable, VisionNotEnabled) as exc:
        print(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        print("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
