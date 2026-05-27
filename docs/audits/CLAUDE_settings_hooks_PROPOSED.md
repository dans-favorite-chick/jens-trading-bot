# Phoenix targeted-pytest hook — merge notes (PROPOSED, 2026-05-24)

This sibling explains how to take `CLAUDE_settings_hooks_PROPOSED.json` and
land it safely. Do **not** copy the JSON over `~/.claude/settings.json` —
it would replace whatever else is in there.

## Files involved

- `docs/audits/CLAUDE_settings_hooks_PROPOSED.json` — the proposed hook shape
  (this directory, for review).
- `~/.claude/settings.json` — operator's live global settings; **merge into**, do not overwrite.
- `C:\Trading Project\phoenix_bot\.claude\hooks\targeted_pytest.ps1` — the
  PowerShell dispatcher the hook invokes. **Create this file** before
  enabling the hook (script body below).

## What the hook does

After every `Edit`, `Write`, or `MultiEdit` tool call, the PowerShell script:

1. Reads `$env:CLAUDE_TOOL_INPUT` (JSON) to find `file_path`.
2. Exits 0 (no-op) if the path is not a `.py` file under
   `C:\Trading Project\phoenix_bot\`.
3. Routes to a small pytest subset based on the directory:
   - `bots/<name>.py`        → `tests/test_<name>.py`
   - `strategies/<name>.py`  → `tests/test_<name>.py` + `tests/test_strategy_smoke.py`
   - `core/<name>.py`        → `tests/test_<name>.py`
   - anything else under `phoenix_bot/` → exit 0 (operator runs manually)
4. Skips silently if none of the candidate test files exist (so editing a
   brand-new module doesn't block on "file not found").
5. Runs `python -m pytest <files> -q --tb=short` from the repo root.
6. **Exit 2 on pytest failure** — this is the Claude Code contract for
   "tool produced a failing state, surface stderr to the model and force
   the next response to address it." Exit 0 on green.

## Merge procedure into `~/.claude/settings.json`

The live file (as of 2026-05-24) at
`C:\Trading Project\phoenix_bot\.claude\settings.json` only contains
`enabledPlugins`. The proposed change adds a `hooks` block alongside it:

```jsonc
{
  "enabledPlugins": { /* ... existing entries unchanged ... */ },
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "pwsh -NoProfile -ExecutionPolicy Bypass -File \"C:\\Trading Project\\phoenix_bot\\.claude\\hooks\\targeted_pytest.ps1\""
          }
        ]
      }
    ]
  }
}
```

If `~/.claude/settings.json` already has a `hooks.PostToolUse` array, append
the new entry to that array instead of replacing it.

## The dispatcher script (`targeted_pytest.ps1`)

Create `C:\Trading Project\phoenix_bot\.claude\hooks\targeted_pytest.ps1`
with this body (also a PROPOSED artifact — operator should review before
enabling):

```powershell
# Phoenix targeted pytest dispatcher
# Invoked by Claude Code as a PostToolUse hook on Edit/Write/MultiEdit.
# Contract: exit 0 = green / skip; exit 2 = test failure (blocks the edit).

$ErrorActionPreference = 'Stop'
$repo = 'C:\Trading Project\phoenix_bot'

# 1. Parse tool input
try {
    $payload = $env:CLAUDE_TOOL_INPUT | ConvertFrom-Json
    $filePath = $payload.file_path
} catch {
    # Malformed payload — don't block edits on hook infra problems.
    exit 0
}
if (-not $filePath) { exit 0 }

# 2. Only care about .py under phoenix_bot/
if ($filePath -notmatch '\.py$') { exit 0 }
$norm = $filePath -replace '/', '\'
if (-not $norm.ToLower().StartsWith($repo.ToLower())) { exit 0 }

# 3. Route to targeted tests
$rel = $norm.Substring($repo.Length).TrimStart('\')
$parts = $rel -split '\\'
$dir   = $parts[0]
$base  = [System.IO.Path]::GetFileNameWithoutExtension($parts[-1])
$tests = @()
switch ($dir) {
    'bots'       { $tests += "tests\test_$base.py" }
    'strategies' { $tests += "tests\test_$base.py"; $tests += 'tests\test_strategy_smoke.py' }
    'core'       { $tests += "tests\test_$base.py" }
    default      { exit 0 }
}

# 4. Keep only tests that exist
$existing = @()
foreach ($t in $tests) {
    $full = Join-Path $repo $t
    if (Test-Path $full) { $existing += $t }
}
if ($existing.Count -eq 0) {
    Write-Host "[phoenix-hook] no matching tests for $rel — skipping"
    exit 0
}

# 5. Run pytest
Push-Location $repo
try {
    Write-Host "[phoenix-hook] running: python -m pytest $($existing -join ' ') -q --tb=short"
    & python -m pytest @existing -q --tb=short
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -ne 0) {
    Write-Host "[phoenix-hook] FAIL ($code) — blocking edit. Fix the targeted test before continuing."
    exit 2
}
exit 0
```

## Verification checklist before enabling

- [ ] `python -m pytest tests/test_risk_manager.py -q --tb=short` passes
      standalone (sanity that the routing target works).
- [ ] Manually invoke the script with a fake payload:
      `$env:CLAUDE_TOOL_INPUT = '{"file_path":"C:\\Trading Project\\phoenix_bot\\core\\risk_manager.py"}'; pwsh -File .claude\hooks\targeted_pytest.ps1; $LASTEXITCODE`
      → should print the pytest command and exit 0 on a green tree.
- [ ] Repeat with a `bots/sim_bot.py` payload.
- [ ] Repeat with a non-Python path (`docs/foo.md`) → exit 0, no pytest run.
- [ ] Repeat with a path outside `phoenix_bot/` → exit 0, no pytest run.
- [ ] After enabling, intentionally break one assertion in a covered file
      and confirm the next Edit is blocked with the failure surfaced.

## Rollback

Either delete the `hooks` block from `~/.claude/settings.json`, or rename
the dispatcher script — the hook will exit non-zero on missing-file, which
Claude Code treats as hook infra failure (NOT exit-2 test failure). If
that turns out to be too disruptive, the dispatcher itself can be
short-circuited with `exit 0` at the top during incident triage.
