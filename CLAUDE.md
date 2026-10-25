# Notes for Claude sessions

- Agent behaviour (batching, prompt, posting rules, chat style, follow/coordinator, the Claude run) lives in
  **chat-agent** (`../chat-agent`), shared with whatsapp-reader. Change it there, not here; this repo only has
  the Discord transport and `src/discord_reader/chat.py`, the adapter. Run both readers' tests after a
  chat-agent change.
