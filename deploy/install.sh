#!/bin/sh
# Install the wrapper and the systemd user service (no root needed). Run on the server from the checkout.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
[ -f "$here/../config.toml" ] || { echo "config.toml missing (copy config.example.toml)" >&2; exit 1; }
for bin in claude gh uv; do command -v "$bin" >/dev/null || { echo "$bin not found in PATH" >&2; exit 1; }; done
mkdir -p ~/.local/bin ~/.config/systemd/user
install -m 755 "$here/discord-reader-wrapper.sh" ~/.local/bin/discord-reader
install -m 644 "$here/discord-reader-agent.service" ~/.config/systemd/user/discord-reader-agent.service  # a copy: `systemctl disable` deletes a linked unit
systemctl --user daemon-reload
systemctl --user enable --now discord-reader-agent.service
systemctl --user --no-pager status discord-reader-agent.service | head -5
