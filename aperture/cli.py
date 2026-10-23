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
from .redact import redact, resolve_boxes
from .scanner import ocr_available, scan

BAR = "-" * 72


def _gate(decider: Decider, columns: int, rows: int, describe: bool = False,
          precise: bool = False) -> RegionGate:
    # screen_kind is a 7-option choice that is informational only -- it never fires a
    # rule. Measured, it adds 5.9s to the context pass (10.0s -> 15.9s), so it is
    # opt-in via --describe rather than paid for on every run.
    return RegionGate(
        decider,
        rules=REGION_RULES,
        context_rules=CONTEXT_RULES,
        info_questions=SCREEN_KIND if describe else None,
        policy=Policy(fail_closed=True),
        # Subdividing twice rather than once takes the covered area from ~24% of a
        # screen to ~14%, and roughly doubles the number of local decisions. Measured
        # on the reported case: 11 calls vs 23.
        refine_depth=2 if precise else 1,
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
        print(f"\n  regions: {result.selection_summary(total)}")
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

    result = _gate(decider, args.columns, args.rows, describe=args.describe, precise=args.precise).inspect(image, hint_boxes=hints)
    _print_result(result, image)

    if args.out and result.verdict in (Verdict.REDACT, Verdict.WITHHOLD):
        boxes, narrowed = resolve_boxes(result.boxes(), hints)
        if result.verdict is Verdict.WITHHOLD:
            boxes = [(0, 0, image.width, image.height)]
        redaction = redact(image, boxes, mode=args.mode)
        redaction.image.save(args.out)
        print(f"\n  wrote redacted copy to {args.out} "
              f"({redaction.fraction_of(image):.0%} of the frame covered)")

    # The annotated view: what the gate saw, and what it decided, drawn over the screen
    # it was looking at. Everything else about this gate is invisible.
    if args.explain:
        from .overlay import annotate
        from .tiling import tile_grid

        tiles = tile_grid(image, columns=args.columns, rows=args.rows)
        shown = annotate(image, result, tiles, title=path.name)
        shown.save(args.explain)
        print(f"  wrote annotated view to {args.explain}")
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
        # The agent is the thing being judged, so its run should produce the same
        # annotated figure that `check --explain` does. The gate fires once per
        # capture, and this is the only moment where both the result and the buffered
        # image are in scope together.
        if args.explain and session.buffer.image is not None:
            from .overlay import annotate

            shown = annotate(
                session.buffer.image,
                result,
                session.buffer.tiles,
                title=args.question[:60],
            )
            shown.save(args.explain)
            print(f"  wrote annotated view to {args.explain}\n")

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


def _vault_from(args: argparse.Namespace):
    """Build a Vault if the user asked for one. Off unless requested."""
    if not getattr(args, "vault", None):
        return None
    from .vault import DEFAULT_DIRECTORY, Vault

    # `--vault` with no value means the default location; `--vault PATH` overrides it.
    directory = None if args.vault is True else args.vault
    return Vault(
        directory or DEFAULT_DIRECTORY,
        keep=args.vault_keep,
        max_age_days=args.vault_days,
    )


def cmd_guard(args: argparse.Namespace) -> int:
    from .watch import guard_clipboard, guard_folders

    vault = _vault_from(args)
    if args.folders or args.dirs:
        return guard_folders(
            directories=[Path(d) for d in args.dirs] if args.dirs else None,
            decider_url=args.decider_url,
            minutes=args.for_minutes,
            columns=args.columns,
            rows=args.rows,
            mode=args.mode,
            use_ocr=args.ocr,
            replace=args.replace,
            notify=args.notify,
        )
    return guard_clipboard(
        decider_url=args.decider_url,
        minutes=args.for_minutes,
        columns=args.columns,
        rows=args.rows,
        mode=args.mode,
        use_ocr=args.ocr,
        notify=args.notify,
        vault=vault,
        describe=args.describe,
    )


# --------------------------------------------------------------------------- doctor


def cmd_vault(args: argparse.Namespace) -> int:
    from .vault import DEFAULT_DIRECTORY, Vault

    store = Vault(args.path or DEFAULT_DIRECTORY)
    if not store.directory.exists():
        print(f"no quarantine folder at {store.directory}")
        print("turn it on with: aperture guard --vault")
        return 0

    entries = store.entries()
    print(store.banner())
    for warning in store.warnings:
        print(f"  WARNING: {warning}")

    if args.purge:
        removed = store.empty()
        print(f"\ndeleted {removed} capture{'' if removed == 1 else 's'}")
        return 0

    if not entries:
        print("\nempty")
    else:
        print(f"\n{'CAPTURE':<34}{'SIZE':>12}{'VERDICT':>12}  WHY")
        print("-" * 96)
        for entry in entries:
            reason = "; ".join(entry.reasons)[:34] or "-"
            print(f"{entry.image_path.name:<34}"
                  f"{entry.size[0]}x{entry.size[1]:<7}"
                  f"{entry.verdict:>12}  {reason}")
        print("\nThese are UNREDACTED originals. Empty with: aperture vault --purge")

    if args.open_it:
        import subprocess

        subprocess.run(["explorer", str(store.directory)], check=False)
    return 0


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
    check.add_argument("--explain", metavar="PATH",
                       help="write an annotated view showing every region's scores and "
                            "what was decided about it")
    check.add_argument("--mode", choices=["block", "blur"], default="block")
    check.add_argument("--no-ocr", dest="ocr", action="store_false")
    check.add_argument("--precise", action="store_true",
                       help="subdivide twice instead of once: covers ~14%% of the "
                            "screen instead of ~24%%, at roughly double the local "
                            "decisions")
    check.add_argument("--describe", action="store_true",
                       help="also ask what kind of screen it is. Informational only, "
                            "and it costs about 6 seconds")
    check.set_defaults(func=cmd_check, ocr=True)

    ask = sub.add_parser("ask", help="run the agent against a screen")
    ask.add_argument("question")
    ask.add_argument("--screen", help="an image to use instead of a live capture")
    ask.add_argument("--model", default=None)
    ask.add_argument("--explain", metavar="PATH",
                     help="write an annotated view of what the gate decided during "
                          "this run")
    ask.set_defaults(func=cmd_ask)

    guard = sub.add_parser("guard", help="watch the clipboard, or screenshot folders")
    guard.add_argument("--for", dest="for_minutes", type=int, default=0,
                       help="stop after N minutes (0 = until Ctrl+C)")
    guard.add_argument("--mode", choices=["block", "blur"], default="block")
    guard.add_argument("--no-ocr", dest="ocr", action="store_false")
    guard.add_argument("--folders", action="store_true",
                       help="watch screenshot folders instead of the clipboard "
                            "(Win+PrtScn and Save-as write files, not clipboard images)")
    guard.add_argument("--dir", dest="dirs", action="append", metavar="PATH",
                       help="watch this directory instead of the platform defaults; "
                            "repeatable. Implies --folders.")
    guard.add_argument("--replace", action="store_true",
                       help="with --folders, overwrite the original instead of writing "
                            "a .safe.png copy beside it")
    guard.add_argument("--no-notify", dest="notify", action="store_false",
                       help="do not show the on-screen panel; console log only")
    guard.add_argument("--vault", nargs="?", const=True, default=None, metavar="PATH",
                       help="keep the ORIGINAL of every blocked capture in a local "
                            "quarantine folder, so you do not lose the screenshot. "
                            "Writes unredacted secrets to disk. Default location is "
                            "~/.aperture/quarantine")
    guard.add_argument("--vault-keep", type=int, default=20, metavar="N",
                       help="keep only the newest N captures (default 20)")
    guard.add_argument("--vault-days", type=int, default=7, metavar="D",
                       help="delete captures older than D days (default 7)")
    guard.add_argument("--describe", action="store_true",
                       help="also ask what kind of screen it is. Informational only, "
                            "and it costs about 6 seconds per capture")
    guard.set_defaults(func=cmd_guard, ocr=True, notify=True)

    vault = sub.add_parser("vault", help="inspect or empty the quarantine folder")
    vault.add_argument("--path", default=None,
                       help="a quarantine folder other than the default")
    vault.add_argument("--purge", action="store_true",
                       help="delete every capture in it")
    vault.add_argument("--open", dest="open_it", action="store_true",
                       help="open the folder in Explorer")
    vault.set_defaults(func=cmd_vault)

    doctor = sub.add_parser("doctor", help="check the moving parts")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def _force_utf8_output() -> None:
    """Stop a stray character in the model's reply from killing the process.

    Windows consoles default to cp1252. Nova emitted a non-breaking hyphen (U+2011) in
    one run and `print` raised UnicodeEncodeError, so the agent's entire answer was lost
    and the command exited 1. A generative model will eventually produce an em dash, a
    curly quote or an emoji; none of those should be fatal to a CLI.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
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
