"""One-off helper (2026-04-25) — patches every scripts/register_*.ps1 to
use a $TaskUser parameter (default "TradingPC\\Trading PC") instead of
$env:USERDOMAIN\\$env:USERNAME, so registering from dbren elevated PS
produces tasks that fire under the actual interactive user.

Idempotent: re-running on already-patched scripts is a no-op.

Usage: python tools/_patch_register_scripts.py
"""
from pathlib import Path
import re
import sys

SCRIPTS = Path("scripts")
TASKUSER_DEFAULT = '"TradingPC\\Trading PC"'
USERDOMAIN_PATTERN = '"$env:USERDOMAIN\\$env:USERNAME"'


def patch_param_block(text: str) -> tuple[str, bool]:
    """Add `[string]$TaskUser = "TradingPC\\Trading PC"` to the param() block.
    Idempotent — does nothing if $TaskUser is already declared."""
    if "$TaskUser" in text:
        return text, False

    # Find the closing `)` of the param() block. Conservatively pick the FIRST
    # one (param blocks are at top of file, before any function defs).
    # Insert the new param before the closing `)`.
    m = re.search(r'param\(\s*\n((?:\s*\[\w+\]\$\w+[^\n]*\n)+)(\s*)\)', text)
    if not m:
        return text, False
    body = m.group(1)
    indent = m.group(2)

    # The last line of body has no trailing comma — add one.
    lines = body.rstrip("\n").split("\n")
    if not lines[-1].rstrip().endswith(","):
        lines[-1] = lines[-1].rstrip() + ","
    # Match the indent of existing param lines (find a [string] line)
    sample_indent_match = re.match(r'(\s*)', lines[0])
    param_indent = sample_indent_match.group(1) if sample_indent_match else "    "
    lines.append(f"{param_indent}[string]$TaskUser = {TASKUSER_DEFAULT}")
    new_body = "\n".join(lines) + "\n"

    new_text = text[:m.start()] + f"param(\n{new_body}{indent})" + text[m.end():]
    return new_text, True


def patch_userdomain_uses(text: str) -> tuple[str, int]:
    """Replace every occurrence of "$env:USERDOMAIN\\$env:USERNAME" with $TaskUser."""
    new_text = text.replace(USERDOMAIN_PATTERN, "$TaskUser")
    count = text.count(USERDOMAIN_PATTERN)
    return new_text, count


def main() -> int:
    if not SCRIPTS.is_dir():
        print(f"ERROR: {SCRIPTS} not found", file=sys.stderr)
        return 1

    files = sorted(SCRIPTS.glob("register_*.ps1"))
    if not files:
        print(f"No register_*.ps1 files in {SCRIPTS}")
        return 1

    total_patched = 0
    for f in files:
        text = f.read_text(encoding="utf-8")
        orig = text
        text, added_param = patch_param_block(text)
        text, replaced_count = patch_userdomain_uses(text)
        if text != orig:
            f.write_text(text, encoding="utf-8")
            total_patched += 1
            print(f"  PATCHED  {f.name}  (param_added={added_param}, "
                  f"USERDOMAIN_replacements={replaced_count})")
        else:
            print(f"  SKIPPED  {f.name}  (already clean)")
    print(f"\n{total_patched}/{len(files)} files patched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
