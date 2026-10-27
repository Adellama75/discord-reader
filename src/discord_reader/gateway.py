"""Push instead of polling: hold a Discord Gateway connection and sync the channel the moment
something happens in it.

The Gateway is only a doorbell. Every event for the channel triggers the same REST `sync` the
poller runs, so the archive, its cursor and catch-up stay exactly as they are: on every
(re)connect a sync fetches whatever arrived while the connection was down.
"""

import json
import random
import threading
import time

from websockets.sync.client import connect

from . import config as cfg
from .sync import sync, sync_reactions

GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
INTENTS = 1 | 512 | 1024  # GUILDS | GUILD_MESSAGES | GUILD_MESSAGE_REACTIONS (content comes over REST)
NEW = {"MESSAGE_CREATE"}
CHANGED = {"MESSAGE_UPDATE", "MESSAGE_DELETE", "MESSAGE_DELETE_BULK",
           "MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE", "MESSAGE_REACTION_REMOVE_ALL", "MESSAGE_REACTION_REMOVE_EMOJI"}


def handle(c: cfg.Config, client, ev: dict) -> str | None:
    """Act on one Gateway dispatch: a new message syncs, an edit, deletion or reaction reconciles,
    READY (a fresh connection) does both to catch up. Returns what it did, for logs and tests."""
    t, d = ev.get("t"), ev.get("d") or {}
    if t == "READY":
        sync(c, client)
        sync_reactions(c, client)
        return "catch-up"
    if d.get("channel_id") != c.channel:
        return None
    if t in NEW:
        sync(c, client)
        return "sync"
    if t in CHANGED:
        sync_reactions(c, client)
        return "reconcile"
    return None


def session(c: cfg.Config, client, token: str, log=print) -> None:
    """One Gateway connection, until Discord closes it or asks for a reconnect. Every `c.poll * 20`
    seconds without events it syncs anyway, so a silently dead socket costs minutes, not messages."""
    with connect(GATEWAY, max_size=None, open_timeout=30) as ws:
        interval = json.loads(ws.recv())["d"]["heartbeat_interval"] / 1000
        seq, stop = [None], threading.Event()

        def beat():
            stop.wait(interval * random.random())
            while not stop.is_set():
                ws.send(json.dumps({"op": 1, "d": seq[0]}))
                stop.wait(interval)

        threading.Thread(target=beat, daemon=True).start()
        ws.send(json.dumps({"op": 2, "d": {"token": token, "intents": INTENTS,
                                           "properties": {"os": "linux", "browser": "discord-reader", "device": "discord-reader"}}}))
        try:
            while True:
                try:
                    ev = json.loads(ws.recv(timeout=c.poll * 20))
                except TimeoutError:
                    sync(c, client)
                    continue
                seq[0] = ev.get("s") or seq[0]
                if ev["op"] == 1:
                    ws.send(json.dumps({"op": 1, "d": seq[0]}))
                elif ev["op"] in (7, 9):  # reconnect / invalid session: start a new one
                    return
                elif ev["op"] == 0:
                    did = handle(c, client, ev)
                    if did == "catch-up":
                        log(f"(connected as {ev['d']['user']['username']}; caught up)")
        finally:
            stop.set()


def watch(c: cfg.Config, client, token: str, log=print) -> None:
    """Keep the archive current over the Gateway, reconnecting forever."""
    while True:
        try:
            session(c, client, token, log)
        except Exception as e:  # dropped socket, DNS, Discord 5xx: the next connect catches up
            log(f"(gateway dropped: {e!r}; reconnecting)")
        time.sleep(5)
