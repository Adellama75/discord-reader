"""The chat-agent adapter for one Discord channel: `discord-reader agent` and `follow` run chat-agent on it."""

from datetime import datetime
from zoneinfo import ZoneInfo

from chatagent import Chat, Msg, core

from . import config as cfg
from .store import (Message, delete_messages, last_reaction_seq, last_seq, message_by_id, messages_after, messages_before,
                    reactions_after, record_send, sent_recently, transcript, update_content)
from .sync import sync, sync_reactions


def is_bot(m: Message, me: str, prefix: str) -> bool:
    """A message the agent itself posted: from this account and carrying the prefix (empty for a
    bot account, so every post of its own counts)."""
    return m.author_id == me and m.content.startswith(prefix)


def replies_to_bot(m: Message, me: str, prefix: str) -> bool:
    """A Discord reply to one of the agent's own posts (same test as is_bot, on the quoted message)."""
    return m.reply_author_id == me and (m.reply_to or "").partition(": ")[2].startswith(prefix)


class DiscordChat(Chat):
    platform, issue_label, who_is_me = "Discord", "discord", ""

    def __init__(self, c: cfg.Config, client, prefix: str = ""):
        self.c, self.client, self.prefix = c, client, prefix
        who = client.me()
        self.me = who["id"]
        self.names = (who.get("username", ""),) if getattr(client, "bot", True) else ()  # a bot answers to its @username
        self.label, self.data_dir, self.attachments = c.label, c.data_dir, str(c.attachments_dir)
        self.tools = [f"Bash(discord-reader {v}:*)" for v in ("send", "react", "edit", "delete", "nick")]
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

    # --- writes (chatagent.Chat contract) -----------------------------------
    def send(self, text: str, reply_to: str | None = None) -> str:
        text = text.strip()
        if not text:
            raise ValueError("nothing to send")
        if len(text) > 1950:
            raise ValueError(f"too long for Discord ({len(text)} > 1950 characters)")
        if reply_to is not None and not str(reply_to).isdigit():
            raise ValueError(f"reply-to wants a message id (digits), got {reply_to!r}")
        limit = self.c.agent.max_sends_per_hour if self.c.agent else 8
        n = sent_recently(self.c.db, self.c.channel, 3600, source="claude")
        if n >= limit:
            raise RuntimeError(f"rate limit: {n} messages sent to {self.label} in the last hour (max {limit})")
        m = self.client.send(self.c.channel, text, reply_to=reply_to)
        record_send(self.c.db, self.c.channel, text, m.get("id"), source="claude")
        return m.get("id")

    def react(self, msg_id: str, emoji: str, *, remove: bool = False) -> None:
        self.client.react(self.c.channel, str(msg_id), emoji, remove=remove)

    def edit(self, msg_id: str, text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("nothing to edit to")
        if len(text) > 1950:
            raise ValueError(f"too long for Discord ({len(text)} > 1950 characters)")
        m = self.client.edit(self.c.channel, str(msg_id), text)
        update_content(self.c.db, str(msg_id), text)  # the archive shows the edit straight away
        return m.get("id")

    def delete(self, msg_id: str) -> None:
        self.client.delete(self.c.channel, str(msg_id))
        delete_messages(self.c.db, [str(msg_id)])  # and drops the message straight away

    # Discord-only (not a Chat contract method): the bot's own server nickname.
    def nick(self, name: str | None = None) -> str:
        """Set the bot's server nickname ("" clears it); with None, just read it. Returns the nickname now
        in effect ("" for none)."""
        guild = self.client.channel(self.c.channel).get("guild_id")
        if not guild:
            raise RuntimeError("this channel has no guild (nicknames are server-only)")
        if name is None:
            return self.client.member(guild, self.client.me()["id"]).get("nick") or ""
        self.client.nick(guild, name)
        return name
