"""Thin Discord REST client for a bot account (no gateway, no library).

Only what the agent needs: who am I, one channel, its messages, reactions, send one message,
download an attachment. `config.toml` can name another class with the same methods
(`[discord] client = "module:Class"`), e.g. a fake for tests or a different transport.
"""

import importlib
import time
import urllib.parse
from pathlib import Path

import httpx

API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/Adellama75/discord-agent, 0.2)"


class DiscordError(RuntimeError):
    pass


class Client:
    bot = True  # answers to its own @username too

    def __init__(self, token: str):
        """`token` is a bot application token (Developer Portal → Bot), with the Message Content intent."""
        self.http = httpx.Client(timeout=30, headers={"Authorization": "Bot " + token.strip(), "User-Agent": UA})

    def _req(self, method: str, path: str, **kw):
        for _ in range(5):
            r = self.http.request(method, API + path, **kw)
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 1))
                except Exception:
                    wait = float(r.headers.get("Retry-After", 1))
                time.sleep(wait + 0.2)
                continue
            if r.status_code >= 400:
                raise DiscordError(f"Discord {r.status_code} on {method} {path}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise DiscordError(f"rate limited repeatedly on {method} {path}")

    def me(self) -> dict:
        return self._req("GET", "/users/@me")

    def channel(self, cid: str) -> dict:
        return self._req("GET", f"/channels/{cid}")

    def messages(self, cid: str, after: str | None = None, limit: int = 100) -> list[dict]:
        """Raw message objects, oldest first. With `after`: everything newer than that id
        (paged by 100). Without: the latest `limit`."""
        if after is None:
            batch = self._req("GET", f"/channels/{cid}/messages", params={"limit": min(limit, 100)})
            return sorted(batch, key=lambda m: int(m["id"]))
        out: list[dict] = []
        for _ in range(50):
            batch = self._req("GET", f"/channels/{cid}/messages", params={"after": after, "limit": 100})
            batch.sort(key=lambda m: int(m["id"]))
            out += batch
            if len(batch) < 100:
                break
            after = batch[-1]["id"]
        return out

    def reaction_users(self, cid: str, mid: str, emoji: str) -> list[dict]:
        """Who reacted with `emoji` (unicode, or `name:id` for a custom emoji) to one message."""
        return self._req("GET", f"/channels/{cid}/messages/{mid}/reactions/{urllib.parse.quote(emoji)}",
                         params={"limit": 100})

    def send(self, cid: str, text: str, reply_to: str | None = None) -> dict:
        body: dict = {"content": text}
        if reply_to:  # a real reply; no allowed_mentions, so <@id>s in the text and the replied-to author still ping
            body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        return self._req("POST", f"/channels/{cid}/messages", json=body)

    def download(self, url: str, dest: Path, max_bytes: int) -> bool:
        """Save an attachment (CDN, no token needed). False if it is bigger than max_bytes."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        got = 0
        with httpx.stream("GET", url, timeout=60, follow_redirects=True) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes():
                    got += len(chunk)
                    if got > max_bytes:
                        tmp.unlink(missing_ok=True)
                        return False
                    f.write(chunk)
        tmp.replace(dest)
        return True


def load_client(spec: str, token: str):
    """The Client to use: this module's, or `module:Class` from [discord] client."""
    if not spec:
        return Client(token)
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name or "Client")(token)
