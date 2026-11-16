# Set up with Claude

Paste this prompt into Claude Code on the machine you want to set up. It installs slots, asks you
what the machine should share, and asks before changing anything outside slots' own files.

```text
Set up "slots" on this machine. slots shares one machine's heavy work (dev stacks, builds, test
suites) between many Claude Code sessions. Its whole manual, as plain text, is at
https://parth12358.github.io/Slots/wiki/all.txt. Read it before you start.

1. Check that python3 is 3.11 or newer, and that uv or pipx is installed. Install slots with
   `uv tool install git+https://github.com/Parth12358/Slots@prod` (or the pipx equivalent), then
   run `slots --version`.
2. Ask me:
   - which folder holds my work, one folder per ticket or task (the "lanes root"), and how those
     folders are named;
   - how many sessions may run local stacks at once, and how many heavy test runs. Look at this
     machine's cores and memory first and suggest numbers;
   - which things only one run may use at a time (a shared test account, an e2e environment);
   - which containers or ports are shared and must never be stopped.
3. Run `slots init`, then edit ~/.config/slots/config.toml to match my answers. Show me the
   result.
4. Run `slots claude install --dry-run` and show me the diff. Install the hooks only after I say
   yes. Then run `slots claude skill` and `slots man --install`.
5. If I already use agent-slot.sh or slot.sh, run `slots migrate --from <that folder>` (a dry
   run) and show me the output. Ask before `--apply` and before `slots shims install`.
6. Check that it works: `slots status`, then `slots run <test pool> -- echo ok`. Tell me how to
   open the dashboard (`slots`) and where the manual is (`slots help`).

Don't change any other settings, and don't stop any process or container.
```

Without Claude: [quickstart](quickstart.md).
