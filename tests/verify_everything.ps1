# Full verification, unattended, in dependency order.
#
# Everything here needs the vision server on :8099 and runs the real model, so it is
# slow -- roughly 80 minutes on a CPU-only machine. Nothing in it is interactive.
#
# Order is deliberate: the cheap static checks first so a syntax error does not surface
# after forty minutes of model calls, then the test suite, then the benchmark, then the
# end-to-end agent run which needs Bedrock as well as the local model.
#
#   .\.venv\Scripts\python.exe ... | Tee-Object keeps the log on disk, because the
#   terminal buffer does not survive long enough to read afterwards.

$ErrorActionPreference = "Continue"
$log = "evalset\out\_verify_everything.txt"
$python = ".\.venv\Scripts\python.exe"

function Step($name, $command) {
    $line = "=" * 74
    Add-Content $log "`n$line`n  $name`n  started $(Get-Date -Format 'HH:mm:ss')`n$line"
    Write-Host "`n>>> $name"
    $output = & ([scriptblock]::Create($command)) 2>&1 | Out-String
    Add-Content $log $output
    Add-Content $log "  exit code: $LASTEXITCODE  finished $(Get-Date -Format 'HH:mm:ss')"
    Write-Host $output
}

Set-Content $log "verification run started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

Step "1. doctor: server, OCR, Bedrock reachability" `
    "$python -m snapguard.cli doctor"

Step "2. offline test suite (no model, no network)" `
    "$python -m pytest tests -q --ignore=tests/test_overlay_redacts.py --ignore=tests/test_gate_smoke.py"

Step "3. gate smoke test (real model)" `
    "$python -u -m pytest tests/test_gate_smoke.py -q"

Step "4. overlay leak test (real model + OCR)" `
    "$python -u -m pytest tests/test_overlay_redacts.py -q"

Step "5. eval set, 12 screens (real model)" `
    "$python -u evalset/bench.py --skip-cloud"

Step "6. agent end to end (local gate + Amazon Bedrock)" `
    "$python -u -m snapguard.cli ask 'my deploy is failing, what is wrong?' --screen evalset/out/deploy_failure.png"

Add-Content $log "`nverification run finished $(Get-Date -Format 'HH:mm:ss')"
Write-Host "`nDONE -- full log at $log"
