---
agent: swing-trader
venture: swing
read_scope_version: 1
---

# Read scope — Claude1 swing-trading agent

This file declares which parts of the Obsidian vault at `c:/Users/User/Desktop/Obsidian/Bertieboo/` this agent is allowed to read.

**Important:** this file governs **cross-project vault access only**. The agent operates freely within its own working directory at `c:/Users/User/Desktop/Claude1/` — read-scope does not constrain local project files (journals, positions, routines, scripts).

If a tool returns an out-of-scope vault file anyway, **stop**, do not summarise or use the content, and surface to Bertrand.

## Allowed scopes (vault)

Pages whose frontmatter `scope:` matches one of the following are in-scope:

- `cross` — cross-venture knowledge (concepts, entities, sources, schema, base patterns). Default for pages without an explicit `scope:` field.
- `swing` — this agent's own venture.

## Allowed vault paths

These globs are in-scope (subject to the Forbidden rules below):

- `wiki/concepts/**` — all concepts
- `wiki/entities/**` — all entities
- `wiki/sources/**` — all sources
- `wiki/projects/swing.md` — swing-trading venture page *(does not yet exist; allowed when created)*
- `wiki/notes/swing-*.md` — swing-prefixed notes *(none yet)*
- `wiki/notes/*.md` excluding files prefixed with another venture key (`eins-*`, `kintsukuroi-*`, `murall-*`, `personal-*`)
- `CLAUDE.md`, `index.md`, `log.md` — vault schema, catalog, history

## Forbidden in vault

Out of scope regardless of path:

- Any page whose frontmatter `scope:` names a different venture: `eins`, `kintsukuroi`, `murall`, `personal`.
- Any page with `scope: confidential` unless this agent's `agent: swing-trader` or `venture: swing` appears in that page's `scope_for:` list.
- `wiki/projects/eins.md` (scope: eins).
- `wiki/projects/kintsukuroi.md`, `wiki/projects/murall.md` (when they exist).
- `wiki/notes/eins-*.md`, `wiki/notes/kintsukuroi-*.md`, `wiki/notes/murall-*.md`, `wiki/notes/personal-*.md`.
- `raw/` — never read raw source files directly. The synthesis in `wiki/sources/` is what you want.

## Particularly useful cross-venture content for this agent

Curated entry points relevant to swing-trading automation work:

- `wiki/notes/claude-code-deployment-guide.md` — the three native Claude Code deployment paths (`/loop`, Scheduled Tasks, Cloud Routines, Modal/Trigger.dev + Agent SDK). Direct migration material for the current Windows Task Scheduler setup.
- `wiki/notes/base-skills-library.md` — cross-venture subagent archetypes and workflow patterns extracted from the three existing Claude Code projects (including this one — but the patterns there are shape-only, not Claude1-specific operational data).
- `wiki/concepts/claude-skills.md` — three-layer skill model + WAT framework + scripts-in-skills + compounding loop. Apply the scripts-in-skills pattern to repeated Python in `trade-researcher` and `risk-and-compliance` subagents.
- `wiki/concepts/hooks.md` — Claude Code event-driven primitive (deterministic side effects on pre/post-tool-use, session events). Candidate for compliance audit trails.
- `wiki/entities/claude-agent-sdk.md` — the SDK, including the May 13 2026 budget update (Claude monthly credit can fund SDK calls).
- `wiki/entities/claude-code.md` — native deployment surface section.

## Overrides

If this agent legitimately needs to read material *outside the vault* AND *outside its own project* (e.g. a public GitHub repo for reference, a Windows Task Scheduler config file in a system directory), list those paths here with a one-line justification each.

- (none)

## On boundary violation

If a Read tool call returns content from an out-of-scope vault file:

1. **Stop processing** the current task.
2. **Do not** summarise, quote, paraphrase, or use the returned content.
3. **Report to Bertrand**: the path returned, the file's `scope:` value (or "absent → default cross"), and why the access was out of scope.
4. **Wait** for an explicit override before continuing.

## Scope on writes (to the vault)

This agent's primary work is in its own project folder. Writing to the vault should be the **exception**, not the rule.

If this agent does write a vault page (e.g. a trading-strategy synthesis), set `scope: swing` for venture-bound content. Use `scope: cross` only when the content is clearly venture-agnostic — and even then, prefer to leave vault writes to Bertrand-supervised sessions in the vault itself rather than ad-hoc writes from this project.

Never create vault pages without an explicit `scope:` field.

## Related

- Vault-side schema: `c:/Users/User/Desktop/Obsidian/Bertieboo/CLAUDE.md` §3 frontmatter `scope:` field.
- Read-scope template (canonical): `c:/Users/User/Desktop/Obsidian/Bertieboo/.claude/skills/_templates/read-scope.md`.
- Architectural context: `c:/Users/User/Desktop/Obsidian/Bertieboo/wiki/notes/vault-as-cross-agent-memory.md`.
