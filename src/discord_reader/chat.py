"""The chat-agent adapter for one Discord channel: `discord-reader agent` and `follow` run chat-agent on it."""

from datetime import datetime
from zoneinfo import ZoneInfo

from chatagent import Msg, core

from . import config as cfg
from .store import Message, last_reaction_seq, last_seq, message_by_id, messages_after, messages_before, reactions_after, transcript
from .sync import sync, sync_reactions


def is_bot(m: Message, me: str, prefix: str) -> bool:
    """A message the agent itself posted: from this account and carrying the prefix (empty for a
    bot account, so every post of its own counts)."""
    return m.author_id == me and m.content.startswith(prefix)


def replies_to_bot(m: Message, me: str, prefix: str) -> bool:
    """A Discord reply to one of the agent's own posts (same test as is_bot, on the quoted message)."""
    return m.reply_author_id == me and (m.reply_to or "").partition(": ")[2].startswith(prefix)


class DiscordChat:
    platform, issue_label, who_is_me = "Discord", "discord", ""

    def __init__(self, c: cfg.Config, client, prefix: str = ""):
        self.c, self.client, self.prefix = c, client, prefix
        who = client.me()
        self.me = who["id"]
        self.names = (who.get("username", ""),) if getattr(client, "bot", True) else ()  # a bot answers to its @username
        self.label, self.data_dir, self.attachments = c.label, c.data_dir, str(c.attachments_dir)
        self.tools = ["Bash(discord-reader send:*)"]
        self.rules = core.posting_rules('discord-reader send "<text>"')
        self.rcursor = last_reaction_seq(c.db)

    def _msg(self, m: Message) -> Msg:
        return Msg(m.seq, m.content, is_bot(m, self.me, self.prefix), replies_to_bot(m, self.me, self.prefix), m)

    def poll(self) -> None:
        sync(self.c, self.client)
        sync_reactions(self.c, self.client)

    def after(self, cursor: int) -> list[Msg]:
        return [self._msg(m) for m in messages_after(self.c.db, self.c.channel, cursor)]

    def before(self, cursor: int, n: int) -> list[Msg]:
        return [self._msg(m) for m in messages_before(self.c.db, self.c.channel, cursor, n)]

    def last(self) -> int:
        return last_seq(self.c.db)

    def render(self, msgs: list[Msg], to_agent=None, ids: bool = False) -> str:
        flag = (lambda m: replies_to_bot(m, self.me, self.prefix)) if to_agent else None
        return transcript([m.raw for m in msgs], self.c.names, self.c.timezone, flag, ids=ids)

    def side_events(self) -> list[str]:
        """Reactions since the last call, one line each (they are rare and short, so they stream at once)."""
        zone, out = ZoneInfo(self.c.timezone), []
        for r in reactions_after(self.c.db, self.rcursor):
            self.rcursor = r.seq
            target = message_by_id(self.c.db, r.message_id)
            if r.user_id == self.me or target is None:
                continue
            snippet = target.content.replace("\n", " ")[:60]
            out.append(f"[{datetime.fromtimestamp(r.ts, zone):%a %d %b %H:%M}] {self.c.names.get(r.user_id, r.user)} reacted "
                       f"{r.emoji.split(':')[0]} to {self.c.names.get(target.author_id, target.author)}: {snippet}")
        return out
