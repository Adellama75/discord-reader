#!/bin/sh
# ~/.local/bin/discord-reader: load the env file, run the CLI from its checkout, from any directory.
set -a; [ -f "$HOME/.config/discord-reader/env" ] && . "$HOME/.config/discord-reader/env"; set +a
: "${DISCORD_READER_CONFIG:=$HOME/dev/discord-reader/config.toml}"; export DISCORD_READER_CONFIG
cd "$HOME/dev/discord-reader" && exec "$HOME/.local/bin/uv" run -q discord-reader "$@"
