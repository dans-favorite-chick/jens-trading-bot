"""
Phoenix Bot — Skills Digest

Walks every installed Claude Code plugin's SKILL.md, extracts the YAML
frontmatter (name + description + when-to-use trigger), and emits a
categorized digest tagged with Phoenix-relevant use-cases.

Wired into SessionStart so every session opens with awareness of what
skills exist and which are most useful for current Phoenix work.

Usage:
    python tools/skills_digest.py                   # Print digest to stdout
    python tools/skills_digest.py --markdown        # Markdown output (for SKILLS.md regeneration)
    python tools/skills_digest.py --json            # JSON output (for tooling)
    python tools/skills_digest.py --phoenix-only    # Skip skills unlikely to be used by Phoenix work
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

PLUGIN_CACHE_ROOT = Path.home() / ".claude" / "plugins" / "cache"

# Phoenix-specific tagging: which Phoenix work area each skill is most
# relevant to. Values are heuristic — derived from skill names. Skills
# not in this map fall through to the "general" bucket.
PHOENIX_TAGS = {
    # Trading strategy / modeling
    "backtesting-frameworks":       ["trading", "backtesting"],
    "risk-metrics-calculation":     ["trading", "risk"],
    # Reliability + observability
    "python-resilience":            ["bot-reliability"],
    "python-observability":         ["bot-reliability", "monitoring"],
    "python-error-handling":        ["bot-reliability"],
    "python-resource-management":   ["bot-reliability"],
    "distributed-tracing":          ["monitoring"],
    "prometheus-configuration":     ["monitoring"],
    "grafana-dashboards":           ["monitoring", "dashboard"],
    "slo-implementation":           ["monitoring"],
    # Code quality
    "python-design-patterns":       ["code-quality"],
    "python-anti-patterns":         ["code-quality"],
    "python-code-style":            ["code-quality"],
    "python-type-safety":           ["code-quality"],
    "python-testing-patterns":      ["testing"],
    "python-performance-optimization": ["performance"],
    # Document outputs (grade reports, debriefs, weekly learner reports)
    "pdf":                          ["reports"],
    "xlsx":                         ["reports"],
    "docx":                         ["reports"],
    "pptx":                         ["reports"],
    # Council / AI agents
    "claude-api":                   ["agents", "council"],
    "mcp-builder":                  ["agents", "integrations"],
    # Async + background
    "async-python-patterns":        ["bot-reliability", "performance"],
    "python-background-jobs":       ["bot-reliability"],
    # Frontend (dashboard)
    "frontend-design":              ["dashboard"],
    "web-artifacts-builder":        ["dashboard"],
    "webapp-testing":               ["dashboard", "testing"],
    # Skill / plugin authoring
    "skill-creator":                ["skill-authoring"],
    # Architecture (for council / risk_gate / orchestrator)
    "api-design-principles":        ["architecture"],
    "architecture-patterns":        ["architecture"],
    "saga-orchestration":           ["architecture", "agents"],
    # Communications
    "internal-comms":               ["reports"],
}


@dataclass
class SkillEntry:
    name: str
    description: str
    plugin: str
    marketplace: str
    path: Path
    phoenix_tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.phoenix_tags = PHOENIX_TAGS.get(self.name, [])

    @property
    def is_phoenix_relevant(self) -> bool:
        return bool(self.phoenix_tags)


# ─── Frontmatter parser ───────────────────────────────────────────

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL
)


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm = m.group(1)
    out = {}
    for line in fm.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _walk_skills(root: Path = PLUGIN_CACHE_ROOT) -> Iterable[SkillEntry]:
    if not root.exists():
        return
    for skill_md in root.rglob("SKILL.md"):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        # Path layout: <root>/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md
        try:
            parts = skill_md.relative_to(root).parts
            marketplace = parts[0]
            plugin = parts[1]
        except (IndexError, ValueError):
            marketplace = "unknown"
            plugin = "unknown"
        yield SkillEntry(
            name=fm.get("name") or skill_md.parent.name,
            description=(fm.get("description") or "").strip().strip('"').strip("'"),
            plugin=plugin,
            marketplace=marketplace,
            path=skill_md,
        )


# ─── Output formats ───────────────────────────────────────────────

def _emit_text(skills: list[SkillEntry], phoenix_only: bool) -> str:
    lines = []
    by_tag: dict[str, list[SkillEntry]] = {}
    untagged = []
    for s in skills:
        if not s.phoenix_tags:
            untagged.append(s)
            continue
        for t in s.phoenix_tags:
            by_tag.setdefault(t, []).append(s)
    lines.append(f"Phoenix Skills Digest — {len(skills)} skills across {len({s.plugin for s in skills})} plugins")
    lines.append("=" * 72)
    for tag in sorted(by_tag.keys()):
        lines.append(f"\n## [{tag}]")
        for s in sorted(by_tag[tag], key=lambda x: x.name):
            desc = (s.description[:90] + "...") if len(s.description) > 90 else s.description
            lines.append(f"  {s.name:<32} {desc}")
    if not phoenix_only and untagged:
        lines.append(f"\n## [general / unmapped] ({len(untagged)} skills)")
        for s in sorted(untagged, key=lambda x: x.name):
            desc = (s.description[:90] + "...") if len(s.description) > 90 else s.description
            lines.append(f"  {s.name:<32} {desc}")
    return "\n".join(lines)


def _emit_markdown(skills: list[SkillEntry]) -> str:
    by_tag: dict[str, list[SkillEntry]] = {}
    untagged = []
    for s in skills:
        if not s.phoenix_tags:
            untagged.append(s)
            continue
        for t in s.phoenix_tags:
            by_tag.setdefault(t, []).append(s)
    lines = [
        "# Phoenix Skills Reference",
        "",
        "_Auto-generated by `tools/skills_digest.py --markdown`. Regenerate any time a new plugin is installed._",
        "",
        f"**{len(skills)} skills** across **{len({s.plugin for s in skills})} plugins** "
        f"from **{len({s.marketplace for s in skills})} marketplaces**.",
        "",
        "## Phoenix-relevant skills, grouped by use-case",
        "",
    ]
    for tag in sorted(by_tag.keys()):
        lines.append(f"### `{tag}`")
        lines.append("")
        lines.append("| Skill | Description | Plugin |")
        lines.append("|---|---|---|")
        for s in sorted(by_tag[tag], key=lambda x: x.name):
            desc = s.description.replace("|", "\\|")[:120]
            lines.append(f"| `{s.name}` | {desc} | {s.plugin} |")
        lines.append("")
    if untagged:
        lines.append(f"## Other available skills ({len(untagged)} skills, no Phoenix-specific use-case mapped yet)")
        lines.append("")
        lines.append("| Skill | Description | Plugin |")
        lines.append("|---|---|---|")
        for s in sorted(untagged, key=lambda x: x.name):
            desc = s.description.replace("|", "\\|")[:120]
            lines.append(f"| `{s.name}` | {desc} | {s.plugin} |")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## How to invoke a skill")
    lines.append("")
    lines.append("In a Claude Code session, ask the assistant to use the skill by name or context. Examples:")
    lines.append("")
    lines.append("- *\"Use the **risk-metrics-calculation** skill to compute Sharpe + max drawdown for the last 30 trades in `logs/trade_memory.json`.\"*")
    lines.append("- *\"Use **pdf** to extract the regime signals from `data/menthorq/morning_brief.pdf`.\"*")
    lines.append("- *\"Use **frontend-design** to add a sentiment-flow widget to dashboard.html.\"*")
    lines.append("")
    return "\n".join(lines)


def _emit_json(skills: list[SkillEntry]) -> str:
    return json.dumps([{
        "name": s.name,
        "description": s.description,
        "plugin": s.plugin,
        "marketplace": s.marketplace,
        "path": str(s.path),
        "phoenix_tags": s.phoenix_tags,
    } for s in skills], indent=2)


# ─── CLI ──────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--markdown", action="store_true", help="Markdown output")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--phoenix-only", action="store_true", help="Only Phoenix-tagged skills")
    parser.add_argument("--out", type=str, default=None, help="Write to file instead of stdout")
    args = parser.parse_args()

    skills = list(_walk_skills())
    if not skills:
        print("(no skills found — Claude Code plugin cache empty?)", file=sys.stderr)
        return 1

    if args.markdown:
        out = _emit_markdown(skills)
    elif args.json:
        out = _emit_json(skills)
    else:
        out = _emit_text(skills, phoenix_only=args.phoenix_only)

    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
