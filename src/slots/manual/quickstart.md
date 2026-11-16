# Quick start

## Install

Needs Python 3.11 or newer on macOS or Linux. No root, no services to set up.

```bash
uv tool install git+https://github.com/Parth12358/Slots@prod     # or: pipx install git+...
slots --version
```

## Configure

```bash
slots init          # writes ~/.config/slots/config.toml, with comments
$EDITOR ~/.config/slots/config.toml
```

Set `lanes.root` to the folder that holds one folder per piece of work (a "lane", usually a
ticket), and size the pools for your machine. See [config](config.md).

## Use

```bash
slots run test -- npx vitest run        # hold a test slot for one command; exits with its code
slots acquire agent --wait              # hold an agent slot across commands (waits in line)
slots release agent                     # give it back once your processes have stopped
slots status                            # who holds and awaits what, lanes, ports, machine
slots                                   # the live dashboard (in a terminal)
```

The background daemon starts by itself the first time a command needs it, so there's nothing to
install as a service.

## Get help

```bash
slots help             # the topic list
slots help agents      # the rules for agents
slots man --install    # then: man slots
```
