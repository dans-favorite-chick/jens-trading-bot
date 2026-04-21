# TODO: Retire C:\Trading Project\trading_bot_project\ properly

Created: 2026-04-18, during NT8 OneDrive → local Documents migration.

## Current state
- Separate git repo at C:\Trading Project\trading_bot_project\
- ~20 untracked files (CLAUDE.md, PROJECT_MEMORY.md, Jen_Trading_Botv1/,
  Afterhours_Test_Bot_v3/, Jen_Trading_Botv2/, Research_Bot_v2/,
  NinjaTrader/, 7x launch_*.bat, misc .py scripts, levels.txt,
  ROADMAP.md, logs/, .claude/)
- Modified but uncommitted: main.py, .langgraph_api state
- Deleted but unstaged: mnq_trading_bot/ tree (bot.py etc.)
- Contains broken OneDrive paths in:
    Jen_Trading_Botv1/trading_controller.py (lines 54-55)
    Jen_Trading_Botv1/_LEGACY_V1/trading_controller.py (lines 53-54)
    CLAUDE.md (2 path refs)
    PROJECT_MEMORY.md (7 path refs)

## Per STRATEGY_KNOWLEDGE_INJECTION_PROMPT.md:98, this system is retired and MUST NOT be
referenced. Broken paths are therefore not a live runtime bug — they
only fire if someone actively violates the "never reference" rule.

## Decisions needed in a future session
1. Commit untracked work OR drop it — which files are worth preserving?
2. Archive strategy — branch + delete from disk, or move to external
   archive location (`~/Archive/` outside trading workspace)?
3. If keeping on disk, add a big `RETIRED.md` banner at repo root.

## Do NOT
- Run any code in trading_bot_project/ until resolved
- Attempt retirement as a drive-by in an unrelated PR
- Reference trading_bot_project/ from phoenix_bot/
