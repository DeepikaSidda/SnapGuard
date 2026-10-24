"""A full sweep of everything SnapGuard claims to do, run against live services.

Not a unit test. This exercises the real CLI surface, the real local model and the real
Bedrock endpoint, and prints a pass/fail line per claim. It exists because "did you test
everything?" deserves an answer with evidence rather than recollection.

Slow by nature: every gate call is ~12 s on CPU, so budget ten minutes.

    .\\.venv\\Scripts\\python.exe tests\\verify_all.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
SNAPGUARD = ROOT / ".venv" / "Scripts" / "snapguard.exe"
SCREENS = ROOT / "evalset" / "out"

results: list[tuple[str, bool, str]] = []


def record(claim: str, ok: bool, detail: str = "") -> None:
    results.append((claim, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {claim}")
    if detail:
        print(f"         {detail}")


def run(args: list[str], timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run(
        [str(SNAPGUARD), *args], capture_output=True, text=True, timeout=timeout, cwd=ROOT
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    started = time.time()
    print("=" * 74)
    print("  SNAPGUARD -- full verification sweep")
    print("=" * 74)

    # ---- 1. the moving parts -------------------------------------------------
    print("\n1. Environment")
    code, out = run(["doctor"], timeout=300)
    for part in ("decider server", "OCR fast lane", "clipboard", "Amazon Bedrock"):
        section = out.split(part, 1)
        ok = len(section) > 1 and "ok " in section[1][:80]
        record(f"doctor: {part}", ok)

    # ---- 2. the gate, one verdict per screen --------------------------------
    print("\n2. The gate: every verdict path")
    expectations = {
        "docs": "PASS",                 # public documentation
        "deploy_failure": "REDACT",     # credentials in one pane
        "bank": "WITHHOLD",             # the whole frame is the problem
    }
    for screen, want in expectations.items():
        code, out = run(["check", str(SCREENS / f"{screen}.png")])
        got = next(
            (ln.split("VERDICT:")[1].strip() for ln in out.splitlines() if "VERDICT:" in ln),
            "none",
        )
        record(f"check {screen}.png -> {want}", got == want, f"got {got}")

    # ---- 3. the deterministic fast lane ------------------------------------
    print("\n3. Fast lane (OCR + regex)")
    code, out = run(["check", str(SCREENS / "deploy_failure.png")])
    record(
        "OCR+regex localises the AWS key",
        "aws_access_key_id" in out,
        next((ln.strip() for ln in out.splitlines() if "aws_access_key_id" in ln), ""),
    )
    record("the secret itself is never printed", "AKIAIOSFODNN7EXAMPLE" not in out)

    # ---- 4. redaction -------------------------------------------------------
    print("\n4. Redaction")
    for mode in ("block", "blur"):
        out_path = SCREENS / f"_verify_{mode}.png"
        code, out = run(
            ["check", str(SCREENS / "deploy_failure.png"), "--out", str(out_path),
             "--mode", mode]
        )
        record(f"--mode {mode} writes a redacted copy", out_path.exists())
        if out_path.exists():
            out_path.unlink()

    # ---- 5. the agent, both branches ---------------------------------------
    print("\n5. The agent on Amazon Bedrock")
    code, out = run(
        ["ask", "my deploy is failing, what's wrong?",
         "--screen", str(SCREENS / "deploy_failure.png")]
    )
    # Deliberately not asserting on a literal string. The first version of this check
    # looked for "PoolTimeout" and failed on a run where the agent paraphrased it as
    # "connection pool ... timeout of 5.0 seconds" -- a correct diagnosis that the test
    # called a failure. A generative model is allowed to choose its own words; what
    # matters is that it identified an exhausted connection pool rather than inventing
    # something, or saying nothing because its answer crashed the terminal.
    lower = out.lower()
    markers = [m for m in ("pooltimeout", "pool", "connection", "timeout", "migrat")
               if m in lower]
    record(
        "agent identified the connection-pool error",
        len(markers) >= 3 and "WHAT THE AGENT SAID" in out,
        f"matched {markers}",
    )
    record("the agent's reply survived printing", code == 0,
           "non-zero exit means output encoding killed it" if code else "")
    record("gate narrowed the call via Transform", "narrowed" in out)
    record("agent was told what was withheld", "withheld" in out.lower())
    narrowed_line = next((ln.strip() for ln in out.splitlines() if "narrowed" in ln), "")
    record("not every region was sent", "allowed [0, 1, 2, 3]" not in narrowed_line,
           narrowed_line)

    code, out = run(
        ["ask", "whats on my screen?", "--screen", str(SCREENS / "bank.png")]
    )
    record("agent denied a fully sensitive screen", "denied" in out)
    record("nothing was sent on a denial", "0 of 4" in out)

    # ---- 6. the reusable pattern in isolation ------------------------------
    print("\n6. The reusable pattern, imported on its own")
    probe = (
        "import sys; sys.path.insert(0, r'%s');"
        "from snapguard.patterns import RegionGate, Rule, Verdict, Policy;"
        "from snapguard.patterns.intervention import ScreenGate, ScreenBuffer;"
        "from snapguard.decider import Decider, noul, looks_blank;"
        "from PIL import Image;"
        "print('blank-guard', looks_blank(Image.new('RGB',(300,300),'white')));"
        "print('verdicts', [v.value for v in Verdict])" % ROOT
    )
    proc = subprocess.run([str(PY), "-c", probe], capture_output=True, text=True)
    record("pattern imports standalone", proc.returncode == 0,
           proc.stdout.strip().replace("\n", " | ") or proc.stderr[:120])
    record("blank-image guard fires without a model call",
           "blank-guard True" in proc.stdout)

    # ---- 7. unit + smoke tests ---------------------------------------------
    print("\n7. Test suite")
    proc = subprocess.run(
        [str(PY), "-m", "pytest", "tests/test_gate_smoke.py",
         "tests/test_clipboard.py", "-q"],
        capture_output=True, text=True, cwd=ROOT, timeout=1800,
    )
    tail = [ln for ln in proc.stdout.splitlines() if "passed" in ln or "failed" in ln]
    record("pytest", proc.returncode == 0, tail[-1] if tail else "")

    # ---- summary -----------------------------------------------------------
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 74)
    print(f"  {passed}/{len(results)} checks passed in {time.time() - started:.0f}s")
    failures = [c for c, ok, _ in results if not ok]
    if failures:
        print("\n  FAILED:")
        for claim in failures:
            print(f"    - {claim}")
    print("=" * 74)
    print("\n  Not covered here, and labelled untested in the code:")
    print("    - macOS clipboard (NSPasteboard): no Apple hardware available")
    print("    - the hosted community endpoint: no usable API key")
    print("  Covered manually, not re-run here (they need a live screen or filesystem):")
    print("    - snapguard ask with live capture (mss)")
    print("    - snapguard guard (clipboard) and guard --folders [--replace]")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
