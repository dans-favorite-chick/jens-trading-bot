# Scratch Directories (not tracked in git)

These directories live in the working tree but are excluded via `.gitignore`.
They hold experimental work, runtime state, or binary artifacts that do not
belong in version control.

_Last updated: 2026-04-18_

---

## `Phoenix Rising Project/`

**Created:** 2026-04-11 (draft session before the MenthorQ integration work began)

Earlier exploratory copy of `agents/` containing older versions of:

- `council_gate.py`
- `pretrade_filter.py`
- `session_debriefer.py`
- `ai_client.py`
- `__init__.py`

These predate the current MenthorQ-aware versions under `agents/`. No active
code imports from this path; it is kept on disk as a reference in case an
older prompt needs to be recovered. If it remains unused long-term, delete
it — nothing in the live codebase depends on it.

## `data/`

Runtime state, caches, and vector databases. The folder itself is kept in
git (via `data/.gitkeep`) because runtime code expects it to exist on a
fresh clone. Contents are ignored:

| Pattern                          | What it holds                                                   |
| -------------------------------- | --------------------------------------------------------------- |
| `data/*.json`                    | aggregator state (lab/prod), `menthorq_daily.json`, `momentum_scores.json` — rewritten every session |
| `data/knowledge_vectors/`        | ChromaDB sqlite database for strategy knowledge (large, binary) |
| `data/trade_vectors/`            | ChromaDB sqlite database for trade RAG (large, binary)          |
| `data/**/*.sqlite`, `*.db`, `*.parquet` | Pre-emptive coverage for future binary storage formats   |

If you need to seed a fresh clone, generate the JSONs by running the bot
once (or copy them from a healthy environment) — they are derivable state,
not source of truth.
