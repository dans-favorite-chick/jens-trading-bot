"""List pending AI proposals (S9).

Usage: python tools/list_proposals.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.adaptive_params import PROPOSALS_DIR, REJECTED_LOG


_STATUS_RE = re.compile(r"^- \*\*Status:\*\*\s*(.+)$", re.MULTILINE)


def main() -> int:
    print(f"Proposals dir: {PROPOSALS_DIR}")
    if not PROPOSALS_DIR.exists():
        print("  (no proposals yet)")
    else:
        mds = sorted(PROPOSALS_DIR.glob("proposal_*.md"))
        if not mds:
            print("  (empty)")
        for md in mds:
            try:
                text = md.read_text(encoding="utf-8")
            except Exception as e:
                print(f"  {md.name}  [read error: {e}]")
                continue
            m = _STATUS_RE.search(text)
            status = m.group(1).strip() if m else "UNKNOWN"
            pid = md.stem.replace("proposal_", "")
            print(f"  [{status:18s}] {pid}")

    if REJECTED_LOG.exists():
        try:
            n = sum(1 for _ in open(REJECTED_LOG, "r", encoding="utf-8"))
        except Exception:
            n = "?"
        print(f"\nRejected log: {REJECTED_LOG}  ({n} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
