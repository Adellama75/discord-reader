import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as cfg
from .discord import load_client
from .store import last_seq, messages_after, messages_before, messages_since, record_send, sent_recently, transcript


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
    who = ", ".join((r.get("global_name") or r["username"]) for r in ch.get("recipients", []))
    print(f"account: {u.get('global_name') or u['username']} (@{u['username']})")
    print(f"channel: {ch.get('name') or c.label} [{c.channel}] with {who}")
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


def cmd_send(c: cfg.Config, args) -> None:
    """Post one message to the configured channel, under the hourly rate limit."""
    text = args.text if args.text is not None else sys.stdin.read()
    text = text.strip()
    if not text:
        sys.exit("nothing to send")
    if len(text) > 1950:
        sys.exit(f"too long for Discord ({len(text)} > 1950 characters)")
    limit = c.agent.max_sends_per_hour if c.agent else 8
    n = sent_recently(c.db, c.channel, 3600, source="claude")
    if n >= limit:
        sys.exit(f"rate limit: {n} messages sent to {c.label} in the last hour (max {limit})")
    if args.reply_to and not args.reply_to.isdigit():
        sys.exit(f"--reply-to wants a message id (digits), got {args.reply_to!r}")
    m = _client(c).send(c.channel, text, reply_to=args.reply_to)
    record_send(c.db, c.channel, text, m.get("id"), source="claude")
    ref = (m.get("message_reference") or {}).get("message_id")
    print(f"sent {m.get('id')}" + (f" (reply to {ref})" if ref else ""))


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

    t = sub.add_parser("transcript", help="print the archived chat")
    t.add_argument("--days", type=float, default=1)
    t.add_argument("--last", type=int, help="the last N messages instead of a period")
    t.add_argument("--no-sync", action="store_true", help="don't poll Discord first")
    t.set_defaults(fn=cmd_transcript)

    s = sub.add_parser("send", help="post one message to the configured channel")
    s.add_argument("text", nargs="?", help="message text (default: stdin)")
    s.add_argument("--reply-to", metavar="MESSAGE_ID", help="post as a Discord reply to this message id")
    s.set_defaults(fn=cmd_send)

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
