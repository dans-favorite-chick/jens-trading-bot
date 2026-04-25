# Claude Code Plugin & Skill Audit

**Generated:** 2026-04-25
**Scope:** Read-only reconnaissance — no installs, modifications, or deletions performed
**Host:** Windows (user `Trading PC`)
**Project:** `C:\Trading Project\phoenix_bot\`

---

## Summary

- **5 plugins installed** (all project-scoped to Phoenix; all enabled)
- **32 skills available** across those plugins (29 from `claude-code-workflows`, 1 from `claude-plugins-official`, 2 from `quantitative-trading` plugin)
- **0 Phoenix-specific skills built** (`.claude/skills/` does not exist in the project — expected, none planned in Phoenix yet)
- **3 marketplaces registered** (`claude-plugins-official`, `claude-code-workflows`, `anthropic-agent-skills`)
- **1 marketplace registered but unused**: `anthropic-agent-skills` has 17 skills available but zero installs from it
- **1 critical warning**: Phoenix `.gitignore` excludes `.claude/`, meaning any project-specific skills you build won't be committed (silent team-sharing blocker)

---

## 1. Plugins inventory

All five plugins are **project-scoped** to `C:\Trading Project\phoenix_bot\` and **enabled** in `.claude/settings.json`. Runtime registry (`installed_plugins.json`) matches disk perfectly — no orphans, no mismatches.

| Plugin | Marketplace | Version | Author | Skills | Agents | Commands | Installed |
|---|---|---|---|---|---|---|---|
| **quantitative-trading** | claude-code-workflows | 1.2.2 | Seth Hobson | 2 | 2 | 0 | 2026-04-25 13:44 |
| **python-development** | claude-code-workflows | 1.2.2 | Seth Hobson | 16 | 3 | 1 | 2026-04-25 13:44 |
| **backend-development** | claude-code-workflows | 1.3.1 | Seth Hobson | 9 | 8 | 1 | 2026-04-25 13:44 |
| **observability-monitoring** | claude-code-workflows | 1.2.2 | Seth Hobson | 4 | 4 | 2 | 2026-04-25 13:47 |
| **skill-creator** | claude-plugins-official | _unknown_ | Anthropic | 1 | 0 | 0 | 2026-04-25 13:46 |

All plugin install paths confirmed on disk under `C:\Users\Trading PC\.claude\plugins\cache\<marketplace>\<plugin>\<version>\`.

### Marketplaces registered

| Marketplace | GitHub source | Plugins installed from it |
|---|---|---|
| `claude-plugins-official` | anthropics/claude-plugins-official | 1 (skill-creator) |
| `claude-code-workflows` | wshobson/agents | 4 (the rest) |
| `anthropic-agent-skills` | anthropics/skills | **0** (unused — see Recommended actions) |

---

## 2. Skills inventory (32 total)

Grouped by source. **No project-level skills exist yet** (the Phoenix repo's `.claude/skills/` directory is absent).

### From `quantitative-trading` plugin (claude-code-workflows, 2 skills)

| Skill name | One-line description |
|---|---|
| backtesting-frameworks | Build robust backtesting systems for trading strategies with proper handling of look-ahead bias, survivorship... |
| risk-metrics-calculation | Calculate portfolio risk metrics including VaR, CVaR, Sharpe, Sortino, and drawdown analysis. Use when measuring... |

### From `python-development` plugin (claude-code-workflows, 16 skills)

| Skill name | One-line description |
|---|---|
| async-python-patterns | Master Python asyncio, concurrent programming, and async/await patterns for high-performance applications. |
| python-anti-patterns | Use when reviewing Python code for common anti-patterns to avoid. Use as a checklist when reviewing... |
| python-background-jobs | Python background job patterns including task queues, workers, and event-driven architecture. |
| python-code-style | Python code style, linting, formatting, naming conventions, and documentation standards. |
| python-configuration | Python configuration management via environment variables and typed settings. |
| python-design-patterns | Python design patterns including KISS, Separation of Concerns, Single Responsibility, and composition over inheritance. |
| python-error-handling | Python error handling patterns including input validation, exception hierarchies, and partial failure handling. |
| python-observability | Python observability patterns including structured logging, metrics, and distributed tracing. |
| python-packaging | Create distributable Python packages with proper project structure, setup.py/pyproject.toml, and publishing. |
| python-performance-optimization | Profile and optimize Python code using cProfile, memory profilers, and performance best practices. |
| python-project-structure | Python project organization, module architecture, and public API design. |
| python-resilience | Python resilience patterns including automatic retries, exponential backoff, timeouts, and fault-tolerant decorators. |
| python-resource-management | Python resource management with context managers, cleanup patterns, and streaming. |
| python-testing-patterns | Implement comprehensive testing strategies with pytest, fixtures, mocking, and test-driven development. |
| python-type-safety | Python type safety with type hints, generics, protocols, and strict type checking. |
| uv-package-manager | Master the uv package manager for fast Python dependency management, virtual environments, and modern Python projects. |

### From `backend-development` plugin (claude-code-workflows, 9 skills)

| Skill name | One-line description |
|---|---|
| api-design-principles | Master REST and GraphQL API design principles to build intuitive, scalable, and maintainable APIs. |
| architecture-patterns | Implement proven backend architecture patterns including Clean Architecture, Hexagonal Architecture, and DDD. |
| cqrs-implementation | Implement Command Query Responsibility Segregation for scalable architectures. |
| event-store-design | Design and implement event stores for event-sourced systems. |
| microservices-patterns | Design microservices architectures with service boundaries, event-driven communication, and resilience patterns. |
| projection-patterns | Build read models and projections from event streams. |
| saga-orchestration | Implement saga patterns for distributed transactions and cross-aggregate workflows. |
| temporal-python-testing | Test Temporal workflows with pytest, time-skipping, and mocking strategies. |
| workflow-orchestration-patterns | Design durable workflows with Temporal for distributed systems. |

### From `observability-monitoring` plugin (claude-code-workflows, 4 skills)

| Skill name | One-line description |
|---|---|
| distributed-tracing | Implement distributed tracing with Jaeger and Tempo to track requests across microservices. |
| grafana-dashboards | Create and manage production Grafana dashboards for real-time visualization of system metrics. |
| prometheus-configuration | Set up Prometheus for comprehensive metric collection, storage, and monitoring. |
| slo-implementation | Define and implement Service Level Indicators (SLIs) and Service Level Objectives (SLOs) with error budgets. |

### From `skill-creator` plugin (claude-plugins-official, 1 skill)

| Skill name | One-line description |
|---|---|
| skill-creator | Create new skills, modify and improve existing skills, and measure skill performance. |

### Project-level skills

**None.** `C:\Trading Project\phoenix_bot\.claude\skills\` does not exist. The 8-Phoenix-skills set described in your roadmap has not been built yet — this matches expectations.

### User-level skills

**None.** `C:\Users\Trading PC\.claude\skills\` does not exist. All skills are plugin-bundled.

---

## 3. Mismatches & warnings

| Severity | Issue | Detail |
|---|---|---|
| ✅ **Clean** | runtime ↔ disk match | All 5 plugins in `installed_plugins.json` are present at their declared `installPath`. No orphan installs, no missing files. |
| ✅ **Clean** | All enabled in project settings | `.claude/settings.json` has all 5 plugins set to `true` |
| ⚠ **Minor** | `skill-creator` version is `unknown` | The plugin.json file lacks a `version` field. Functionally fine; cosmetic only — registry shows `version: "unknown"`. |
| ⚠ **Wasted opportunity** | `anthropic-agent-skills` marketplace registered but unused | 17 skills available from `anthropics/skills` (`pdf`, `docx`, `xlsx`, `pptx`, `webapp-testing`, `mcp-builder`, etc.) — none installed. Several would be immediately useful for Phoenix's reporting / debrief / dashboard pipelines. |
| 🔴 **Critical for team-share** | Phoenix `.gitignore` excludes `.claude/` | When you eventually build project-level Phoenix skills under `.claude/skills/`, they will NOT be committed to git. This silently blocks team sharing of project skills. **Fix before building any Phoenix skill.** |

---

## 4. Recommended next actions

### 4a. Fix the `.gitignore` blocker (5 sec)

The current rule `.claude/` excludes the entire directory. Replace with a more selective pattern that keeps skills + project settings tracked but ignores worktree-local state:

```bash
# In C:\Trading Project\phoenix_bot\.gitignore — replace ".claude/" with:
.claude/worktrees/
.claude/cache/
.claude/file-history/
.claude/session-env/
.claude/sessions/
.claude/shell-snapshots/
.claude/telemetry/
# Keep .claude/skills/, .claude/settings.json, .claude/agents/, .claude/commands/ tracked
```

After this, when you build the 8 Phoenix skills, they'll commit normally.

### 4b. Useful plugins/skills available but not installed

From `anthropic-agent-skills` (already-registered marketplace, no install yet):

```
/plugin install pdf@anthropic-agent-skills
/plugin install docx@anthropic-agent-skills
/plugin install xlsx@anthropic-agent-skills
/plugin install pptx@anthropic-agent-skills
/plugin install webapp-testing@anthropic-agent-skills
/plugin install mcp-builder@anthropic-agent-skills
```

From `claude-plugins-official` (already-registered):

```
/plugin install code-review@claude-plugins-official      # PR / change review
/plugin install code-simplifier@claude-plugins-official  # mirrors the /simplify slash command
/plugin install pr-review-toolkit@claude-plugins-official
/plugin install commit-commands@claude-plugins-official
/plugin install hookify@claude-plugins-official          # Claude-Code hook scaffolding
/plugin install plugin-dev@claude-plugins-official       # if you build phoenix-trading plugin
/plugin install session-report@claude-plugins-official
/plugin install pyright-lsp@claude-plugins-official      # adds pyright type-check integration
/plugin install ralph-loop@claude-plugins-official       # /loop scaffolding
```

From `claude-code-workflows` (already-registered) — Phoenix-relevant additions:

```
/plugin install machine-learning-ops@claude-code-workflows  # for the FinBERT/HMM Phase B+ work
/plugin install data-engineering@claude-code-workflows      # for trade_memory / chromadb work
/plugin install incident-response@claude-code-workflows     # for the WatcherAgent escalation
/plugin install cicd-automation@claude-code-workflows
/plugin install kubernetes-operations@claude-code-workflows # only if/when you migrate to VPS
/plugin install security-scanning@claude-code-workflows     # would benefit the OIF guard work
```

I'd flag **machine-learning-ops**, **incident-response**, and **pyright-lsp** as the highest-leverage adds for current Phoenix B+ work.

### 4c. The 8 Phoenix skills you mentioned

When you're ready to build them, scaffold under:

```
C:\Trading Project\phoenix_bot\.claude\skills\<skill-name>\SKILL.md
```

Each `SKILL.md` needs YAML frontmatter at top:

```markdown
---
name: phoenix-grader-runner
description: Run Phoenix's open-prediction grader against today's session log. Triggers on phrases like "grade today" or "run P1-P6 check"
---

# Phoenix Grader Runner

(skill body)
```

You already have the `skill-creator` plugin installed, which can scaffold these for you via:

```
/skill-creator new phoenix-grader-runner
```

(Run that and it'll prompt for description / triggers / skill body, then write the file.)

---

## 5. Cross-reference summary

| Check | Result |
|---|---|
| Plugins on disk → registered in `installed_plugins.json` | ✅ All 5 match |
| Plugins registered → present at declared `installPath` | ✅ All 5 match |
| Plugins enabled in project settings | ✅ All 5 enabled |
| Plugin version vs registry version | ✅ Match (skill-creator's `unknown` is the source of truth — manifest lacks version) |
| Skills count vs plugin manifest hint | n/a — plugin.json files don't enumerate bundled skills; counted via filesystem scan (32 total SKILL.md files) |
| Marketplace registrations vs plugins from each | 1 unused (`anthropic-agent-skills`) — opportunity, not a problem |
| Phoenix project skills directory | absent — expected |
| Phoenix `.gitignore` skill commit policy | **broken** — `.claude/` rule prevents future skill commits |

---

## 6. Locations cheat-sheet

```
User-level Claude Code config:
  C:\Users\Trading PC\.claude\
  ├── settings.json                    (16 entries — global)
  ├── plugins\
  │   ├── installed_plugins.json       (5 plugins, all project-scoped)
  │   ├── known_marketplaces.json      (3 marketplaces)
  │   ├── cache\<marketplace>\<plugin>\<version>\     ← actual code
  │   └── marketplaces\<marketplace>\                  ← git-cloned source-of-truth
  └── (no skills/ at user level)

Project-level Claude Code config (per-project):
  C:\Trading Project\phoenix_bot\.claude\
  ├── settings.json                    (5 enabledPlugins)
  ├── worktrees\                       (existing)
  └── (no skills/ — none built yet)
```

— end of audit
