import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as cfg
from .discord import load_client
from .store import last_seq, messages_after, messages_before, messages_since, transcript


def _client(c: cfg.Config):
    return load_client(c.client, cfg.token(c))


def cmd_whoami(c: cfg.Config, args) -> None:
    u = _client(c).me()
    print(f"{u.get('global_name') or u['username']} (@{u['username']}, id {u['id']})")


def cmd_status(c: cfg.Config, args) -> None:
    """Who the token is, what the channel is, how much is archived, where the agent cursor is."""
    client = _client(c)
    u = client.me()
    ch = client.channel(c.channel)
    if ch.get("recipients"):  # a group DM: name the people in it
        where = " with " + ", ".join((r.get("global_name") or r["username"]) for r in ch["recipients"])
    elif ch.get("guild_id"):  # a server channel: name the server
        where = f" in {client.guild(ch['guild_id']).get('name') or ch['guild_id']}"
    else:
        where = ""
    print(f"account: {u.get('global_name') or u['username']} (@{u['username']})")
    print(f"channel: {ch.get('name') or c.label} [{c.channel}]{where}")
    if c.db.exists():
        n = last_seq(c.db)
        recent = messages_before(c.db, c.channel, n + 1, 1)
        last = f"{recent[0].sent_at.astimezone():%Y-%m-%d %H:%M} {recent[0].author}" if recent else "-"
        print(f"archive: {c.db} ({n} rows, last {last})")
    else:
        print(f"archive: {c.db} does not exist yet (run `discord-reader sync`)")
    state = c.data_dir / "agent.json"
    print(f"agent:   cursor file {'present' if state.exists() else 'missing'}; "
          f"{'PAUSED' if (c.data_dir / 'agent.paused').exists() else 'active'} (data/agent.paused)")


def cmd_sync(c: cfg.Config, args) -> None:
    from .sync import sync

    print(f"{sync(c, _client(c))} new messages")


def cmd_transcript(c: cfg.Config, args) -> None:
    """Print the archived chat (after a sync) for the last N days or the last N messages."""
    flag = None
    if not args.no_sync:
        from .chat import replies_to_bot
        from .sync import sync

        client = _client(c)
        sync(c, client)
        if c.agent:  # mark replies to the agent's own posts, as `follow` does
            me, prefix = client.me()["id"], c.agent.prefix
            flag = lambda m: replies_to_bot(m, me, prefix)  # noqa: E731
    if args.last:
        msgs = messages_before(c.db, c.channel, last_seq(c.db) + 1, args.last)
    else:
        msgs = messages_since(c.db, c.channel, datetime.now(timezone.utc) - timedelta(days=args.days))
    print(transcript(msgs, c.names, c.timezone, flag, ids=True) or "No messages")


def _chat(c: cfg.Config):
    from .chat import DiscordChat
    return DiscordChat(c, _client(c), c.agent.prefix if c.agent else "")


def cmd_send(c: cfg.Config, args) -> None:
    """Post one message to the configured channel, under the hourly rate limit."""
    text = args.text if args.text is not None else sys.stdin.read()
    try:
        mid = _chat(c).send(text, reply_to=args.reply_to)
    except (ValueError, RuntimeError) as e:  # DiscordError is a RuntimeError
        sys.exit(str(e))
    print(f"sent {mid}")


def cmd_react(c: cfg.Config, args) -> None:
    """Add (or with --remove, take off) the bot's reaction on a message."""
    try:
        _chat(c).react(args.message_id, args.emoji, remove=args.remove)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
    print(f"{'unreacted' if args.remove else 'reacted'} {args.emoji} on {args.message_id}")


def cmd_edit(c: cfg.Config, args) -> None:
    """Edit one of the bot's own messages."""
    text = args.text if args.text is not None else sys.stdin.read()
    try:
        mid = _chat(c).edit(args.message_id, text)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
    print(f"edited {mid}")


def cmd_delete(c: cfg.Config, args) -> None:
    """Delete one of the bot's own messages."""
    try:
        _chat(c).delete(args.message_id)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
    print(f"deleted {args.message_id}")


def cmd_nick(c: cfg.Config, args) -> None:
    """Set the bot's own server nickname (empty to clear)."""
    try:
        _chat(c).nick(args.name)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
    print("cleared nickname" if not args.name else f"nickname -> {args.name!r}")


def cmd_watch(c: cfg.Config, args) -> None:
    """Keep the archive current over the Gateway (push): sync on every event for the channel and on
    every reconnect. Read-only; run `follow` or `agent` on the archive it keeps."""
    from .gateway import watch

    watch(c, _client(c), cfg.token(c), log=lambda s: print(s, flush=True))


def cmd_follow(c: cfg.Config, args) -> None:
    """Stream each burst of new messages (and reactions) to stdout for a coordinator session; see
    chatagent.follow. Reads the archive the service keeps; polls Discord itself only with --sync."""
    from chatagent import follow

    from .chat import DiscordChat

    chat = DiscordChat(c, _client(c), c.agent.prefix if c.agent else "")
    follow([chat], c.agent, agent_chat=chat, after=args.after, interval=args.interval, polls=args.polls, fetch=args.sync)


def cmd_agent(c: cfg.Config, args) -> None:
    """Watch the channel and run Claude Code on each burst of new messages (chat-agent)."""
    from chatagent import AgentLoop

    from .chat import DiscordChat

    if c.agent is None:
        sys.exit("no [agent] section in config.toml")
    loop = AgentLoop(c.agent, DiscordChat(c, _client(c), c.agent.prefix))
    if args.once or args.dry_run:
        out = loop.once(dry_run=args.dry_run)
        if out:
            print(out)
        return
    loop.serve(interval=args.interval or c.poll, polls=args.polls)


def main() -> None:
    p = argparse.ArgumentParser(prog="discord-reader",
                                description="one Discord group chat -> Claude Code -> GitHub issues and replies")
    # DISCORD_READER_CONFIG lets subprocesses (Claude's `discord-reader send` under `discord-reader agent`) find the same config
    # from any working directory.
    p.add_argument("--config", type=Path, default=Path(os.environ.get("DISCORD_READER_CONFIG", "config.toml")))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="confirm the token works").set_defaults(fn=cmd_whoami)
    sub.add_parser("status", help="account, channel, archive size, agent cursor").set_defaults(fn=cmd_status)
    sub.add_parser("sync", help="fetch new messages into data/messages.db").set_defaults(fn=cmd_sync)
    sub.add_parser("watch", help="keep data/messages.db current over the Gateway (push, no agent)").set_defaults(fn=cmd_watch)

    t = sub.add_parser("transcript", help="print the archived chat")
    t.add_argument("--days", type=float, default=1)
    t.add_argument("--last", type=int, help="the last N messages instead of a period")
    t.add_argument("--no-sync", action="store_true", help="don't poll Discord first")
    t.set_defaults(fn=cmd_transcript)

    s = sub.add_parser("send", help="post one message to the configured channel")
    s.add_argument("text", nargs="?", help="message text (default: stdin)")
    s.add_argument("--reply-to", metavar="MESSAGE_ID", help="post as a Discord reply to this message id")
    s.set_defaults(fn=cmd_send)

    rc = sub.add_parser("react", help="add/remove the bot's reaction on a message")
    rc.add_argument("message_id", help="the message's id (the #id shown by transcript/follow)")
    rc.add_argument("emoji", help="unicode emoji, or name:id for a custom emoji")
    rc.add_argument("--remove", action="store_true", help="take the reaction off instead")
    rc.set_defaults(fn=cmd_react)

    ed = sub.add_parser("edit", help="edit one of the bot's own messages")
    ed.add_argument("message_id", help="the message's id")
    ed.add_argument("text", nargs="?", help="new text (default: stdin)")
    ed.set_defaults(fn=cmd_edit)

    dl = sub.add_parser("delete", help="delete one of the bot's own messages")
    dl.add_argument("message_id", help="the message's id")
    dl.set_defaults(fn=cmd_delete)

    nk = sub.add_parser("nick", help="set the bot's own server nickname (omit name to clear)")
    nk.add_argument("name", nargs="?", default="", help="new nickname; omit to clear")
    nk.set_defaults(fn=cmd_nick)

    f = sub.add_parser("follow", help="stream bursts of new messages to stdout for a coordinator session")
    f.add_argument("--after", type=int, help="start after this archive seq (default: now)")
    f.add_argument("--interval", type=float, default=5, help="seconds between archive checks")
    f.add_argument("--sync", action="store_true", help="poll Discord too (when the service isn't running)")
    f.add_argument("--polls", type=int, help="stop after N checks (tests)")
    f.set_defaults(fn=cmd_follow)

    a = sub.add_parser("agent", help="new messages -> Claude Code -> GitHub issues + replies")
    a.add_argument("--once", action="store_true", help="process what is pending now, then exit")
    a.add_argument("--dry-run", action="store_true", help="print the prompt Claude would get, change nothing")
    a.add_argument("--polls", type=int, help="stop after N polls (default: run forever)")
    a.add_argument("--interval", type=float, help="seconds between polls (default: [discord] poll)")
    a.set_defaults(fn=cmd_agent)

    args = p.parse_args()
    path = args.config.resolve()
    if not path.exists():
        sys.exit(f"{path} not found (copy config.example.toml, or set DISCORD_READER_CONFIG)")
    os.environ["DISCORD_READER_CONFIG"] = str(path)
    args.fn(cfg.load(path), args)
