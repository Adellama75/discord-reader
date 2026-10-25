"""Fetching the channel into data/messages.db: new messages (with small attachments), then a
reconcile pass over the most recent ones for edits, deletions and reactions."""

import re

from . import config as cfg
from .store import (connect, delete_messages, display, from_raw, insert, last_id, message_by_id, reaction_count,
                    set_reactions, stored_ids_since, update_content)


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name)[:80] or "file"


def sync(c: cfg.Config, client) -> int:
    """Pull what is new in the channel into messages.db; save small attachments next to it.
    An empty archive is seeded with the latest 100 messages (context for the first run)."""
    last = last_id(c.db, c.channel)
    raw = client.messages(c.channel, after=last) if last else client.messages(c.channel, limit=100)
    msgs = [from_raw(r) for r in raw]
    cap = int(c.attachments_max_mb * 1024 * 1024)
    for m in msgs:
        for a in m.attachments:
            dest = c.attachments_dir / f"{m.id}_{safe_name(a['name'])}"
            try:
                if dest.exists() or (a.get("size", 0) <= cap and client.download(a["url"], dest, cap)):
                    a["path"] = str(dest)
            except Exception:  # attachment stays as a URL; not worth failing the sync
                pass
    return insert(c.db, msgs)


def sync_reactions(c: cfg.Config, client, recent: int = 30) -> list[tuple[str, str, str, str]]:
    """Re-read the last `recent` messages and reconcile them with the archive: edited text is
    updated and deleted messages are dropped (as whatsapp-reader's bridge does for WhatsApp edits
    and "delete for everyone"), and reactions are synced. Only a (message, emoji) whose count changed
    costs a second request (who reacted). Returns the reactions that were added as
    (message_id, emoji, user_id, user)."""
    added = []
    raws = client.messages(c.channel, limit=recent)
    if raws:  # the latest `recent` messages: anything stored from the oldest of them on and missing was deleted
        live = {r["id"] for r in raws}
        delete_messages(c.db, [i for i in stored_ids_since(c.db, c.channel, raws[0]["id"]) if i not in live])
    for raw in raws:
        m = from_raw(raw)
        if raw.get("edited_timestamp"):
            stored = message_by_id(c.db, m.id)
            if stored and stored.content != m.content:
                update_content(c.db, m.id, m.content)
        for emoji, count in m.reaction_counts.items():
            if reaction_count(c.db, m.id, emoji) != count:
                users = [(u["id"], display(u)) for u in client.reaction_users(c.channel, m.id, emoji)]
                added += [(m.id, emoji, uid, name) for uid, name in set_reactions(c.db, m.id, emoji, users)]
        stored = {e for e, _, _ in messages_by_id_reactions(c, m.id)}
        for emoji in stored - set(m.reaction_counts):
            set_reactions(c.db, m.id, emoji, [])  # all reactions of that emoji were removed
    return added


def messages_by_id_reactions(c: cfg.Config, message_id: str) -> list[tuple[str, str, str]]:
    with connect(c.db) as conn:
        return [tuple(r) for r in conn.execute("SELECT emoji, user_id, user FROM reactions WHERE message_id=?", (message_id,))]
