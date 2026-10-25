"""data/messages.db: the local archive of the one channel the agent watches, plus its own sends.

`sync.sync()` is the only writer of `messages`; `discord-reader send` writes `sends`. Everything
Claude sees is rendered from here by `transcript()`, so the format is stable and testable.
"""

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    author_id TEXT NOT NULL,
    author TEXT NOT NULL,
    ts INTEGER NOT NULL,
    content TEXT NOT NULL,
    attachments TEXT NOT NULL DEFAULT '[]',
    reply_to TEXT
);
CREATE TABLE IF NOT EXISTS reactions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user TEXT NOT NULL,
    ts INTEGER NOT NULL,
    UNIQUE (message_id, emoji, user_id)
);
CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    ts INTEGER NOT NULL,
    text TEXT NOT NULL,
    msg_id TEXT,
    source TEXT NOT NULL DEFAULT 'claude'  -- rate bucket: 'claude' is `discord-reader send`
);
"""

MENTION = re.compile(r"<@!?(\d+)>")


@dataclass
class Message:
    id: str  # Discord snowflake
    channel: str
    author_id: str
    author: str  # display name at the time it was stored
    sent_at: datetime
    content: str  # with <@id> mentions rendered as @name
    attachments: list[dict] = field(default_factory=list)  # {name, url, size, path?}
    reply_to: str | None = None  # "name: snippet" of the message this one replies to
    reply_author_id: str | None = None  # author id of that message
    seq: int = 0
    reactions: list[tuple[str, str, str]] = field(default_factory=list)  # (emoji, user_id, user)
    reaction_counts: dict[str, int] = field(default_factory=dict)  # emoji -> count, as Discord reports it


def display(u: dict) -> str:
    return u.get("global_name") or u.get("username") or u.get("id", "?")


def from_raw(raw: dict) -> Message:
    names = {u["id"]: display(u) for u in raw.get("mentions", [])}
    content = MENTION.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), raw.get("content") or "")
    if raw.get("sticker_items"):
        content = (content + " [sticker]").strip()
    ref = raw.get("referenced_message")
    reply_to = reply_author_id = None
    if ref:
        reply_author_id = ref.get("author", {}).get("id")
        snippet = (ref.get("content") or "").replace("\n", " ")
        reply_to = f"{display(ref.get('author', {}))}: {snippet[:80]}{'…' if len(snippet) > 80 else ''}"
    atts = [{"name": a.get("filename", "file"), "url": a["url"], "size": a.get("size", 0)}
            for a in raw.get("attachments", [])]
    counts = {emoji_key(r["emoji"]): r.get("count", 0) for r in raw.get("reactions", [])}
    return Message(
        id=raw["id"], channel=raw["channel_id"], author_id=raw["author"]["id"], author=display(raw["author"]),
        sent_at=datetime.fromisoformat(raw["timestamp"]), content=content, attachments=atts, reply_to=reply_to,
        reply_author_id=reply_author_id,        reaction_counts=counts,
    )


def emoji_key(e: dict) -> str:
    """Unicode emoji as itself; a custom emoji as `name:id` (what the reactions endpoint takes)."""
    return f"{e['name']}:{e['id']}" if e.get("id") else e["name"]


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    if "reply_author_id" not in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN reply_author_id TEXT")  # archives from before it
    if "source" not in {r[1] for r in conn.execute("PRAGMA table_info(sends)")}:
        conn.execute("ALTER TABLE sends ADD COLUMN source TEXT NOT NULL DEFAULT 'claude'")
    return conn


def _row(r) -> Message:
    return Message(id=r[1], channel=r[2], author_id=r[3], author=r[4],
                   sent_at=datetime.fromtimestamp(r[5], tz=timezone.utc), content=r[6],
                   attachments=json.loads(r[7]), reply_to=r[8], reply_author_id=r[9], seq=r[0])


COLS = "seq, id, channel, author_id, author, ts, content, attachments, reply_to, reply_author_id"


def insert(db: Path, msgs: list[Message]) -> int:
    """Store new messages (oldest first, so seq follows chat order). Returns how many were new."""
    n = 0
    with connect(db) as conn:
        for m in sorted(msgs, key=lambda m: int(m.id)):
            cur = conn.execute(
                "INSERT OR IGNORE INTO messages (id, channel, author_id, author, ts, content, attachments, reply_to,"
                " reply_author_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (m.id, m.channel, m.author_id, m.author, int(m.sent_at.timestamp()), m.content,
                 json.dumps(m.attachments, ensure_ascii=False), m.reply_to, m.reply_author_id))
            n += cur.rowcount
    return n


def last_id(db: Path, channel: str) -> str | None:
    with connect(db) as conn:
        r = conn.execute("SELECT id FROM messages WHERE channel=? ORDER BY seq DESC LIMIT 1", (channel,)).fetchone()
    return r[0] if r else None


def last_seq(db: Path) -> int:
    with connect(db) as conn:
        return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()[0]


def _with_reactions(conn: sqlite3.Connection, msgs: list[Message]) -> list[Message]:
    for m in msgs:
        m.reactions = [tuple(r) for r in conn.execute(
            "SELECT emoji, user_id, user FROM reactions WHERE message_id=? ORDER BY seq", (m.id,))]
    return msgs


def messages_after(db: Path, channel: str, seq: int) -> list[Message]:
    with connect(db) as conn:
        rows = conn.execute(f"SELECT {COLS} FROM messages WHERE channel=? AND seq>? ORDER BY seq", (channel, seq))
        return _with_reactions(conn, [_row(r) for r in rows])


def messages_before(db: Path, channel: str, seq: int, n: int) -> list[Message]:
    with connect(db) as conn:
        rows = conn.execute(f"SELECT {COLS} FROM messages WHERE channel=? AND seq<? ORDER BY seq DESC LIMIT ?",
                            (channel, seq, n))
        return _with_reactions(conn, [_row(r) for r in reversed(rows.fetchall())])


def messages_since(db: Path, channel: str, since: datetime) -> list[Message]:
    with connect(db) as conn:
        rows = conn.execute(f"SELECT {COLS} FROM messages WHERE channel=? AND ts>=? ORDER BY seq",
                            (channel, int(since.timestamp())))
        return _with_reactions(conn, [_row(r) for r in rows])


def reaction_count(db: Path, message_id: str, emoji: str) -> int:
    with connect(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM reactions WHERE message_id=? AND emoji=?",
                            (message_id, emoji)).fetchone()[0]


def set_reactions(db: Path, message_id: str, emoji: str, users: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Make the stored reactors for (message, emoji) equal to `users` [(id, name)]. Returns the ones added."""
    added = []
    with connect(db) as conn:
        have = {r[0] for r in conn.execute("SELECT user_id FROM reactions WHERE message_id=? AND emoji=?",
                                            (message_id, emoji))}
        for uid, name in users:
            if uid not in have:
                conn.execute("INSERT OR IGNORE INTO reactions (message_id, emoji, user_id, user, ts) VALUES (?,?,?,?,?)",
                             (message_id, emoji, uid, name, int(time.time())))
                added.append((uid, name))
        gone = have - {u for u, _ in users}
        if gone:
            conn.executemany("DELETE FROM reactions WHERE message_id=? AND emoji=? AND user_id=?",
                             [(message_id, emoji, u) for u in gone])
    return added


@dataclass
class Reaction:
    seq: int
    message_id: str
    emoji: str
    user_id: str
    user: str
    ts: int


def reactions_after(db: Path, seq: int) -> list[Reaction]:
    with connect(db) as conn:
        return [Reaction(*r) for r in conn.execute(
            "SELECT seq, message_id, emoji, user_id, user, ts FROM reactions WHERE seq>? ORDER BY seq", (seq,))]


def last_reaction_seq(db: Path) -> int:
    with connect(db) as conn:
        return conn.execute("SELECT COALESCE(MAX(seq), 0) FROM reactions").fetchone()[0]


def message_by_id(db: Path, message_id: str) -> Message | None:
    with connect(db) as conn:
        r = conn.execute(f"SELECT {COLS} FROM messages WHERE id=?", (message_id,)).fetchone()
        return _row(r) if r else None


def update_content(db: Path, message_id: str, content: str) -> None:
    """An edited message: keep the latest text (the agent may already have acted on the old one)."""
    with connect(db) as conn:
        conn.execute("UPDATE messages SET content=? WHERE id=?", (content, message_id))


def stored_ids_since(db: Path, channel: str, first: str) -> list[str]:
    """Stored message ids from snowflake `first` on (snowflakes are ordered by time)."""
    with connect(db) as conn:
        rows = conn.execute("SELECT id FROM messages WHERE channel=?", (channel,)).fetchall()
    return [r[0] for r in rows if int(r[0]) >= int(first)]


def delete_messages(db: Path, ids: list[str]) -> None:
    """Deleted on Discord: drop the messages and their reactions, as whatsapp-reader does for revokes."""
    if not ids:
        return
    with connect(db) as conn:
        conn.executemany("DELETE FROM messages WHERE id=?", [(i,) for i in ids])
        conn.executemany("DELETE FROM reactions WHERE message_id=?", [(i,) for i in ids])


def sent_recently(db: Path, channel: str, window_s: int, source: str = "claude") -> int:
    """Sends from one rate bucket in the last window (each bucket has its own hourly cap)."""
    with connect(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM sends WHERE channel=? AND ts>? AND source=?",
                            (channel, int(time.time()) - window_s, source)).fetchone()[0]


def record_send(db: Path, channel: str, text: str, msg_id: str | None, source: str = "claude") -> None:
    with connect(db) as conn:
        conn.execute("INSERT INTO sends (channel, ts, text, msg_id, source) VALUES (?,?,?,?,?)",
                     (channel, int(time.time()), text, msg_id, source))


def transcript(msgs: list[Message], names: dict[str, str], tz: str = "Europe/London",
               to_claude: Callable[[Message], bool] | None = None, ids: bool = False) -> str:
    """`[Mon 20 Oct 17:36] kim: text` per message (`[Mon 20 Oct 17:36 #<id>]` with `ids`, for
    `send --reply-to`); continuation lines indented; files listed
    with their saved path (if downloaded) or URL. Messages `to_claude` picks (replies to the
    agent's own posts) are marked `[to claude]`."""
    zone = ZoneInfo(tz)
    out: list[str] = []
    for m in msgs:
        who = names.get(m.author_id, m.author)
        head = f"[{m.sent_at.astimezone(zone):%a %d %b %H:%M}{f' #{m.id}' if ids else ''}] {who}:"
        if to_claude and to_claude(m):
            head += " [to claude]"
        if m.reply_to:
            head += f" (replying to {m.reply_to})"
        files = "".join(
            f" [file: {a['name']} -> {a['path']}]" if a.get("path") else f" [file: {a['name']} {a['url']}]"
            for a in m.attachments)
        if m.reactions:
            by: dict[str, list[str]] = {}
            for emoji, uid, user in m.reactions:
                by.setdefault(emoji.split(":")[0], []).append(names.get(uid, user))
            files += " [" + "; ".join(f"{e} {', '.join(u)}" for e, u in by.items()) + "]"
        lines = ((m.content.strip() + files).strip() or "(empty)").split("\n")
        out.append(f"{head} {lines[0]}")
        out.extend("    " + line for line in lines[1:])
    return "\n".join(out)
