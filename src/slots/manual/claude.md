# Claude Code

slots works with Claude Code through hooks: each session registers itself, heavy Bash commands
without a slot are stopped with instructions, and a session that ends gives back everything it
held. The dashboard shows every session: its lane, busy or idle, what it holds, what it runs.

## Set up

```bash
slots claude install --dry-run      # show the change to ~/.claude/settings.json
slots claude install                # make it (the old file is backed up in the state folder)
slots claude skill                  # write ~/.claude/skills/local-slots/SKILL.md from your config
```

`--project` uses `./.claude/settings.local.json` and `./.claude/skills/` instead. Your other hooks
and settings are left exactly as they were. `slots claude uninstall` removes only the slots hooks.
New sessions pick the hooks up; restart running ones. Rerun `slots claude skill` after changing
the config, so the limits in it stay right.

## What each hook does

| Claude Code event | slots |
|---|---|
| `SessionStart` | registers the session (its id, its lane from the working folder, its `claude` process), puts `SLOTS_SESSION` and `SLOTS_LANE` in the session's shell, and tells Claude the rules and what its lane holds |
| `UserPromptSubmit` | marks the session busy |
| `PreToolUse` (Bash) | classifies the command; heavy work in a lane with no agent slot is stopped, with the exact commands to get one (see below) |
| `Stop` | marks the session idle |
| `Notification` (`idle_prompt`) | still idle: counted, shown in the dashboard; a desktop notification if `claude.notify` is on |
| `SessionEnd` | releases every lease and wait the session has |

A slot taken inside a session (`slots acquire agent` from its Bash) is tied to the session's
`claude` process, so it's freed when the session ends, crashes or is killed, even if no hook runs.

## Heavy commands

Two lists of patterns in `[claude]` (regexes, matched anywhere in the command):

- `heavy`: one-shot work such as `vitest run`, `playwright test`, `npm run build`, `npm test`.
- `stack`: long-running local stacks such as `docker compose up`, `npm run dev`, `next dev`.

Both need the lane to hold an agent slot (the session's own, or one taken for the lane by hand).
Without one the call is blocked and Claude is told: `slots acquire agent --wait`, then the command
again (as `slots run test -- <command>` for heavy ones).

With `mode = "wrap"`, a heavy command in a lane that holds its agent slot is also rewritten to run
in a test slot: `slots run test --label "<lane> <cmd>" -- bash -c '<command>'`. Commands that
already go through slots or the old scripts (`exempt`) aren't wrapped again; stacks never are.
Wrapping doesn't approve the command: your permission rules still apply. In auto mode Claude Code
may refuse a rewritten command its classifier didn't review, which is why `block` is the default.

Outside lane folders nothing is gated (`lanes_only = true`).

## If something goes wrong

A hook never breaks a session: if the daemon can't be reached or anything fails, the hook lets
the call through and writes the error to `hooks.log` in the state folder. If the daemon forgot a
session (it was reset, or the hooks were installed mid-session), the next hook registers it again.
