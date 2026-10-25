"""A fake Discord and a fake `claude`/`gh` so the loop runs end to end offline.
Never add real chat content to tests."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from discord_reader import config as cfg

CHANNEL = "1000"
ME = "1"
KIM = "2"
LEE = "3"


class FakeDiscord:
    """Enough of discord.Client for the agent: an in-memory channel of raw message objects."""

    def __init__(self, token: str = ""):
        self.msgs: list[dict] = []
        self.sent: list[str] = []
        self.calls: list[tuple] = []
        self._next = 100

    def add(self, author: str, content: str, **extra) -> dict:
        self._next += 1
        name = {ME: "Team Bot", KIM: "kim", LEE: "lee"}[author]
        username = {ME: "teambot", KIM: "kim_dev", LEE: "lee.l"}[author]
        m = {"id": str(self._next), "channel_id": CHANNEL, "content": content,
             "author": {"id": author, "username": username, "global_name": name},
             "timestamp": f"2026-10-20T17:{(self._next % 60):02d}:00.000000+00:00",
             "mentions": [], "attachments": [], **extra}
        self.msgs.append(m)
        return m

    def me(self):
        return {"id": ME, "username": "teambot", "global_name": "Team Bot"}

    def channel(self, cid):
        return {"id": cid, "type": 3, "name": "test group", "recipients": []}

    def messages(self, cid, after=None, limit=100):
        self.calls.append(("messages", after, limit))
        if after is None:
            return self.msgs[-limit:]
        return [m for m in self.msgs if int(m["id"]) > int(after)]

    def react(self, mid: str, emoji: str, *user_ids: str):
        """Add reactions the way Discord reports them: counts on the message, users on request."""
        m = next(x for x in self.msgs if x["id"] == mid)
        self.reactors = getattr(self, "reactors", {})
        self.reactors.setdefault((mid, emoji), []).extend(user_ids)
        m["reactions"] = [{"emoji": {"id": None, "name": e}, "count": len(u)}
                          for (i, e), u in self.reactors.items() if i == mid]

    def reaction_users(self, cid, mid, emoji):
        self.calls.append(("reaction_users", mid, emoji))
        names = {ME: ("teambot", "Team Bot"), KIM: ("kim_dev", "kim"), LEE: ("lee.l", "lee")}
        return [{"id": u, "username": names[u][0], "global_name": names[u][1]}
                for u in getattr(self, "reactors", {}).get((mid, emoji), [])]

    def send(self, cid, text, reply_to=None):
        self.sent.append(text)
        return self.add(ME, text)

    def download(self, url, dest: Path, max_bytes):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")
        return True


@pytest.fixture
def fake():
    return FakeDiscord()


@pytest.fixture
def bin_dir(tmp_path):
    """Fake `claude` (records its prompt, returns JSON) and `gh` (empty issue list) on PATH."""
    b = tmp_path / "bin"
    b.mkdir()
    claude = b / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        f"cat > {tmp_path}/prompt.txt\n"
        f"printf '%s\\n' \"$@\" > {tmp_path}/args.txt\n"
        'echo \'{"result": "nothing to do", "total_cost_usd": 0.01}\'\n')
    gh = b / "gh"
    gh.write_text("#!/bin/sh\necho '[]'\n")
    for f in (claude, gh):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return b


@pytest.fixture
def conf(tmp_path, bin_dir, monkeypatch):
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "token").write_text("x")
    return cfg.Config(
        token_file=tmp_path / "token", channel=CHANNEL, label="test group", data_dir=tmp_path / "data",
        names={ME: "bot"},
        agent=cfg.Agent(repo="org/repo", workdir=repo, debounce=10, max_wait=60, prefix="\U0001f916",
                        max_sends_per_hour=2, claude=str(bin_dir / "claude"),
                        github_users={"bot": "team-bot", "kim": "kim-gh"}),
    )
