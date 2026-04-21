# agents/prompts/

Prompt templates for Phase E-H AI agents (H-4A..4E).

Each sub-stream (S5-S9) will add its own prompt file here. Keep prompts
in plain text / markdown files (not Python) so they can be iterated on
without code changes. Agents load them via:

```python
from pathlib import Path
PROMPT = (Path(__file__).parent / "prompts" / "my_prompt.txt").read_text()
```

## Conventions

- Filename: `{agent_name}_{role}.txt` (e.g. `council_voter_quant.txt`)
- First line = a one-line description (comment) starting with `#`
- System prompt and user prompt may be in the same file separated by
  `---SYSTEM---` / `---USER---` delimiters, or in two files with
  `_system.txt` / `_user.txt` suffixes. Pick one per agent and stay
  consistent within that agent.
- No secrets, no account-specific info, no PII.

## Index

(empty — populated by S5-S9)
