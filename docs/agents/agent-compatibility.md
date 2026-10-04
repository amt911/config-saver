# Agent compatibility — Codex and Claude Code

> Moved verbatim out of `AGENTS.md` on 2026-10-04 so that file fits the 32 KiB Codex reads
> by default. Its rules still bind: `AGENTS.md` lists the hard ones inline and says when to
> read this file. Edit the rule here, not a copy of it.

## Agent compatibility — Codex and Claude Code

This file is `AGENTS.md`: the **one** instruction file for every coding agent in this repo. Codex reads
it directly; Claude Code reads `CLAUDE.md`, which only imports this file (`@AGENTS.md`) and holds what
applies to Claude alone. **Edit rules here, never in `CLAUDE.md`** — two copies of a rule drift apart
on the first edit, and each agent then obeys a different one.

| Concern | Claude Code | Codex |
| --- | --- | --- |
| Instruction file | `CLAUDE.md` → imports `AGENTS.md` | `AGENTS.md` (root down to the working directory) |
| Invoke a skill | `Skill` tool, or `/<skill>` | mention it (`$<skill>`), or let it trigger from its description |
| Skills on disk | `~/.claude/skills` (links into `~/.agents/skills`) | `.agents/skills`, then `~/.agents/skills` |
| superpowers | `superpowers@claude-plugins-official` (`/plugin install`) | `superpowers@openai-curated` (install from `/plugins`; that id is its key in `~/.codex/config.toml`) |
| MCP servers | `claude mcp add -s user <name> -- <cmd>` | `codex mcp add <name> -- <cmd>` (`~/.codex/config.toml`) |
| File size | imports load whole | `project_doc_max_bytes`, **32 KiB by default** — `AGENTS.md` stays ≤ 32 KiB, detail lives in `docs/agents/`; above the limit the tail is silently dropped |

- **Install shared skills once, for both agents:** `npx skills add <owner/repo> -g --skill <name>`
  writes to `~/.agents/skills` and links it for Claude Code, so both run the same version.
- **Names in this file are capabilities, not one agent's syntax.** "Invoke the `X` skill" means the
  `Skill` tool in Claude Code and a skill mention in Codex. An MCP server named here is used when it is
  registered for the agent you are running in; its absence never blocks ordinary work.
- **Modes, model caps and Git rules bind both agents.** "lite mode", "normal mode" and "modo
  desatendido" mean the same in Codex; a cap written as "no model above Sonnet" means "no model above
  the mid tier" there.
- **Claude-only commands** (`/graphify` and other slash commands that are not skills) are skipped by
  Codex unless the same capability is installed as a skill in `~/.agents/skills`.
