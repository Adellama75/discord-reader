# discord-reader

Archives one Discord channel with a bot account, and runs [chat-agent](https://github.com/Adellama75/chat-agent)
on it: every burst of new messages becomes one headless `claude -p` run that keeps a GitHub repo's issues in
step with what the team says and posts short updates back.

```
Discord REST poll (15 s) ─▶ data/messages.db ─▶ chat-agent (batch, prompt, claude -p) ─▶ gh issue create/comment/close
                                                                                   └─▶ discord-reader send ─▶ channel
```

- The archive keeps edits, deletions (of recent messages) and reactions; attachments up to 10 MB are saved
  under `data/attachments/` so Claude can Read screenshots and PDFs.
- `<@id>` mentions and replies are rendered as names; a reply to one of the bot's posts counts as addressing it.
- The agent's behaviour (batching, prompt, chat style, coordinator `follow`) is chat-agent's, shared with
  [whatsapp-reader](https://github.com/Adellama75/whatsapp-reader).

## Setup

1. Create an application and a bot at <https://discord.com/developers/applications>, enable the
   **Message Content** intent, and invite it to your server with *View Channel*, *Read Message History*
   and *Send Messages* on the channel.
2. On the machine that will run it, with [chat-agent](https://github.com/Adellama75/chat-agent) cloned next
   to this repo (`../chat-agent`):

```sh
uv sync
mkdir -p ~/.config/discord-reader && install -m 600 /dev/stdin ~/.config/discord-reader/bot-token <<< "<bot token>"
cp config.example.toml config.toml   # channel id, repo, clone path, brief, name map
uv run discord-reader whoami         # token works?
uv run discord-reader status         # channel, archive, cursor
uv run discord-reader agent --dry-run   # the exact prompt Claude would get right now
./deploy/install.sh                  # wrapper in ~/.local/bin + systemd user service
```

`claude`, `gh` (logged in with write access to the repo) and `uv` must be on `~/.local/bin`.
Claude runs bill the `claude` login on the server (any `ANTHROPIC_API_KEY` in `~/.config/discord-reader/env`
is hidden from the subprocess).

## Day to day

```sh
discord-reader agent --dry-run              # what Claude would see now
tail -f data/agent.log                      # every run: cursors, seconds, cost, result
touch data/agent.paused                     # hold runs, keep polling (rm to resume)
systemctl --user stop discord-reader-agent  # stop entirely
discord-reader send "..." [--reply-to ID]   # post by hand (rate-limited)
discord-reader transcript --last 30         # the archived chat
discord-reader follow                       # stream bursts for a coordinator session
discord-reader watch                        # keep the archive current over the Gateway (push, read-only)
systemctl --user enable --now discord-reader-watch@NAME   # `watch` for ~/.config/discord-reader/NAME.toml
```

Smoke test: write `@claude what's open?` in the channel; it should answer within ~30 s and the run appears in
`data/agent.log`.

`[discord] client = "module:Class"` swaps in another client class with the same methods (tests use a fake).

## Tests

`uv run pytest` runs against a fake Discord and a fake `claude`; nothing leaves the machine. chat-agent has its
own tests for the loop.

## License

AGPL-3.0-or-later.
