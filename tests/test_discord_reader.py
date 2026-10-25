import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import CHANNEL, ME, LEE, KIM

from chatagent import AgentLoop, core

from discord_reader import cli
from discord_reader import config as cfg
from discord_reader.chat import DiscordChat, is_bot, replies_to_bot
from discord_reader.store import Message, from_raw, last_seq, messages_after, transcript
from discord_reader.sync import sync, sync_reactions


def agent_loop(conf, fake):
    return AgentLoop(conf.agent, DiscordChat(conf, fake, conf.agent.prefix))


def msg(text, author=KIM, seq=1):
    return Message("1", CHANNEL, author, "x", datetime(2026, 10, 20, 17, tzinfo=timezone.utc), text, seq=seq)


def test_own_posts_are_the_accounts_prefixed_ones():
    assert is_bot(msg("🤖 Opened #1", author=ME), ME, "🤖")
    assert not is_bot(msg("🤖 Opened #1", author=KIM), ME, "🤖")  # someone else quoting the prefix
    assert not is_bot(msg("I'm in", author=ME), ME, "🤖")  # the owner's own ordinary chat


def test_reply_to_the_agent_counts_as_addressed(conf, fake, monkeypatch, capsys):
    a = cfg.Agent(repo="o/r", workdir=Path("."), prefix="[Claude]:")
    chat = DiscordChat(conf, fake, a.prefix)
    mine = fake.add(ME, "[Claude]: merged #21, v32 is live")
    plain = fake.add(ME, "back in 10")  # an unprefixed post from the same account is not the agent's
    fake.add(KIM, "nice, which build?", referenced_message=mine)
    fake.add(LEE, "ok", referenced_message=plain)
    msgs = [from_raw(m) for m in fake.msgs]
    assert msgs[2].reply_author_id == ME
    assert core.urgent([chat._msg(msgs[2])], a.names) and not core.urgent([chat._msg(msgs[3])], a.names)
    assert replies_to_bot(from_raw(fake.add(KIM, "hi", referenced_message=plain)), ME, "")  # bot mode
    out = transcript(msgs[2:], {}, "UTC", lambda m: replies_to_bot(m, ME, a.prefix))
    assert out.split("\n")[0].startswith("[Tue 20 Oct 17:43] kim: [to claude] (replying to Team Bot: [Claude]:")
    assert "[to claude]" not in out.split("\n")[1]
    # follow flags it and streams it at once, like a name mention
    conf.agent = a
    monkeypatch.setattr(cli, "_client", lambda c: fake)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    sync(conf, fake)
    cli.cmd_follow(conf, type("A", (), {"after": 0, "interval": 0, "sync": False, "polls": 1})())
    assert "kim: [to claude] (replying to" in capsys.readouterr().out


def test_from_raw_renders_mentions_replies_and_attachments():
    raw = {"id": "5", "channel_id": CHANNEL, "content": "<@2> go throw this at claude",
           "author": {"id": "1", "username": "teambot", "global_name": "Ada"},
           "timestamp": "2026-10-20T15:22:00+00:00",
           "mentions": [{"id": "2", "username": "kim_dev", "global_name": "kim"}],
           "attachments": [{"filename": "review.pdf", "url": "https://cdn/x.pdf", "size": 10}],
           "referenced_message": {"content": "a long question\nover lines", "author": {"username": "lee"}}}
    m = from_raw(raw)
    assert m.content == "@kim go throw this at claude"
    assert m.reply_to == "lee: a long question over lines"
    m.attachments[0]["path"] = "/data/5_review.pdf"
    line = transcript([m], {"1": "Ada"}, "Europe/London")
    assert line == "[Tue 20 Oct 16:22] Ada: (replying to lee: a long question over lines) @kim go throw this at claude [file: review.pdf -> /data/5_review.pdf]"
    # follow/transcript print the id inside the bracket, so `\] name:` filters still match
    assert transcript([m], {"1": "Ada"}, "Europe/London", ids=True).startswith(f"[Tue 20 Oct 16:22 #{m.id}] Ada: ")


def test_send_reply_sets_message_reference(monkeypatch):
    from discord_reader.discord import Client
    seen = {}
    monkeypatch.setattr(Client, "_req", lambda self, method, path, **kw: seen.update(kw) or {"id": "9"})
    c = Client.__new__(Client)
    c.send("1", "hi <@2>", reply_to="7")
    assert seen["json"] == {"content": "hi <@2>", "message_reference": {"message_id": "7", "fail_if_not_exists": False}}
    c.send("1", "plain")
    assert seen["json"] == {"content": "plain"}


def test_sync_seeds_latest_then_paginates_in_order(conf, fake):
    for i in range(130):
        fake.add(KIM, f"m{i}")
    assert sync(conf, fake) == 100  # seed: only the latest 100
    assert fake.calls[-1] == ("messages", None, 100)
    assert sync(conf, fake) == 0
    fake.add(LEE, "new one")
    fake.add(KIM, "new two")
    assert sync(conf, fake) == 2
    assert fake.calls[-1][1] == "230"  # after the last stored id
    texts = [m.content for m in messages_after(conf.db, CHANNEL, 0)]
    assert texts[:2] == ["m30", "m31"] and texts[-2:] == ["new one", "new two"]


def test_sync_saves_small_attachments(conf, fake):
    fake.add(KIM, "look", attachments=[{"filename": "a b.png", "url": "https://cdn/a.png", "size": 5},
                                       {"filename": "huge.zip", "url": "https://cdn/h.zip", "size": 10**9}])
    sync(conf, fake)
    m = messages_after(conf.db, CHANNEL, 0)[0]
    assert Path(m.attachments[0]["path"]).read_bytes() == b"png"
    assert "path" not in m.attachments[1]
    assert "[file: huge.zip https://cdn/h.zip]" in transcript([m], {}, "UTC")


def test_first_start_pins_cursor_and_runs_only_on_new(conf, fake, tmp_path):
    fake.add(KIM, "old chatter")
    loop = agent_loop(conf, fake)
    assert loop.pending() == []
    fake.add(KIM, "I'll fix the login timeout by Wednesday")
    assert loop.poll(now=0) is False  # within debounce
    assert loop.poll(now=20) is True
    prompt = (tmp_path / "prompt.txt").read_text()
    assert "## New messages (act on these)\n[Tue 20 Oct" in prompt
    assert "I'll fix the login timeout" in prompt.split("## New messages")[1]
    assert "old chatter" in prompt.split("## New messages")[0]  # context
    args = (tmp_path / "args.txt").read_text()
    assert "Bash(discord-reader send:*)" in args and "org/repo" in args and "kim -> kim-gh" in args
    log = [json.loads(l) for l in (conf.data_dir / "agent.log").read_text().splitlines()]
    assert log[-1]["cost"] == 0.01 and log[-1]["n"] == 1
    assert loop.pending() == []
    assert json.loads((conf.data_dir / "agent.json").read_text())["cursor"] == last_seq(conf.db)


def test_mention_skips_debounce(conf, fake):
    loop = agent_loop(conf, fake)
    fake.add(KIM, "claude what's open?")
    assert loop.poll(now=0) is True


def test_own_prefixed_posts_are_skipped_silently(conf, fake):
    loop = agent_loop(conf, fake)
    fake.add(ME, "🤖 Opened #3 ...")
    assert loop.poll(now=100) is False
    assert loop.pending() == []


def test_dry_run_prints_and_keeps_cursor(conf, fake, capsys):
    loop = agent_loop(conf, fake)
    fake.add(LEE, "can run the tests on my pc tonight")
    sync(conf, fake)
    loop.run(loop.pending(), dry_run=True)
    out = capsys.readouterr().out
    assert "can run the tests" in out and "## Issues in the repo" in out
    assert loop.pending()


def test_send_enforces_rate_limit(conf, fake, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda c: fake)
    ns = lambda text: type("A", (), {"text": text, "reply_to": None})()
    cli.cmd_send(conf, ns("🤖 one"))
    cli.cmd_send(conf, ns("🤖 two"))
    with pytest.raises(SystemExit, match="rate limit"):
        cli.cmd_send(conf, ns("🤖 three"))
    assert fake.sent == ["🤖 one", "🤖 two"]
    with pytest.raises(SystemExit, match="nothing to send"):
        cli.cmd_send(conf, ns("   "))


def test_config_resolves_relative_paths_against_the_file(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[discord]\nchannel = 42\n[paths]\ndata_dir = "d"\n[names]\n7 = "Ada"\n'
        '[agent]\nrepo = "o/r"\nworkdir = "~/x"\nnames = ["bot"]\n')
    c = cfg.load(tmp_path / "config.toml")
    assert c.channel == "42" and c.data_dir == tmp_path / "d" and c.names == {"7": "Ada"}
    assert c.agent.names == ("bot",) and c.agent.workdir == Path.home() / "x"


def test_coordinator_heartbeat_hands_replies_to_the_live_session(conf, fake, tmp_path):
    loop = agent_loop(conf, fake)
    (conf.data_dir / core.HEARTBEAT).touch()
    fake.add(KIM, "@claude what's open?")
    assert loop.poll(now=0) is False  # a mention is no longer urgent: the coordinator answers
    assert loop.poll(now=20) is True  # but the issue bookkeeping still runs after the debounce
    assert "send **no reply**" in (tmp_path / "args.txt").read_text()
    import os, time
    os.utime(conf.data_dir / core.HEARTBEAT, (time.time() - 300, time.time() - 300))
    fake.add(KIM, "@claude and now?")
    assert loop.poll(now=30) is True  # stale heartbeat: back to answering at once
    assert "send **no reply**" not in (tmp_path / "args.txt").read_text()


def test_follow_streams_bursts_and_keeps_heartbeat(conf, fake, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_client", lambda c: fake)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    fake.add(KIM, "seed")
    sync(conf, fake)
    fake.add(ME, "\U0001f916 own post")
    fake.add(KIM, "claude ping")
    sync(conf, fake)
    ns = type("A", (), {"after": 1, "interval": 0, "sync": False, "polls": 1})()  # after the seed row
    cli.cmd_follow(conf, ns)
    out = capsys.readouterr().out
    assert "claude ping" in out and "own post" not in out and "seed" not in out
    assert not (conf.data_dir / core.HEARTBEAT).exists()  # removed on exit


def test_client_uses_bot_auth_and_custom_clients_load_from_config():
    from discord_reader.discord import Client, load_client
    c = Client("t")
    assert c.http.headers["authorization"] == "Bot t" and c.http.headers["user-agent"].startswith("DiscordBot")
    assert isinstance(load_client("", "t"), Client)
    fake = load_client("conftest:FakeDiscord", "t")  # any class with the same methods
    assert type(fake).__name__ == "FakeDiscord"


def test_bot_mode_prompt_and_own_name(conf, fake, tmp_path):
    conf.agent = cfg.Agent(repo="o/r", workdir=tmp_path, prefix="", claude=conf.agent.claude, debounce=10, max_wait=60)
    loop = agent_loop(conf, fake)
    assert "teambot" in loop.names  # the bot answers to its own username
    system = core.system_prompt(loop.a, loop.chat)
    assert "add no prefix" in system and "Example: `Opened #7" in system
    fake.add(KIM, "hey @teambot what's open?")
    assert loop.poll(now=0) is True  # mention of the bot's own name is urgent
    fake.add(ME, "Opened #1 ...")  # everything from the bot's own account is its own post
    assert loop.poll(now=1) is False and loop.pending() == []


def test_reactions_are_synced_rendered_and_streamed(conf, fake, monkeypatch, capsys):
    m = fake.add(KIM, "should we adopt plan_b?")
    sync(conf, fake)
    assert sync_reactions(conf, fake) == []
    fake.react(m["id"], "\U0001f44d", LEE, ME)
    added = sync_reactions(conf, fake)
    assert {(u) for _, _, u, _ in added} == {LEE, ME}
    assert fake.calls[-1] == ("reaction_users", m["id"], "\U0001f44d")
    assert sync_reactions(conf, fake) == []  # unchanged count: no second lookup
    assert fake.calls[-1][0] == "messages"
    line = transcript(messages_after(conf.db, CHANNEL, 0), conf.names, "UTC")
    assert line.endswith("should we adopt plan_b? [\U0001f44d lee, bot]")
    # follow streams the reaction (not the account owner's own) with the target snippet
    monkeypatch.setattr(cli, "_client", lambda c: fake)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    fake.react(m["id"], "\U0001f389", KIM)
    sync_reactions(conf, fake)
    ns = type("A", (), {"after": None, "interval": 0, "sync": False, "polls": 1})()
    monkeypatch.setattr("discord_reader.chat.last_reaction_seq", lambda db: 0)  # follower started before the reactions
    cli.cmd_follow(conf, ns)
    out = capsys.readouterr().out
    assert "lee reacted \U0001f44d to kim: should we adopt plan_b?" in out
    assert "kim reacted \U0001f389 to kim" in out and "bot reacted" not in out


def test_edits_and_deletions_in_recent_messages_reach_the_archive(conf, fake):
    from discord_reader.store import message_by_id
    keep = fake.add(KIM, "meet at 6")
    gone = fake.add(LEE, "oops wrong chat")
    sync(conf, fake)
    keep["content"], keep["edited_timestamp"] = "meet at 7", "2026-10-20T18:00:00+00:00"
    fake.msgs.remove(gone)
    sync_reactions(conf, fake)
    assert message_by_id(conf.db, keep["id"]).content == "meet at 7"
    assert message_by_id(conf.db, gone["id"]) is None


def test_failed_git_pull_is_logged_and_flagged_in_the_prompt(conf, fake, tmp_path):
    loop = agent_loop(conf, fake)  # conf's workdir is not a git repo, so the pull fails
    fake.add(KIM, "@claude what's open?")
    assert loop.poll(now=0) is True
    assert "WARNING: `git pull` in the repo clone failed" in (tmp_path / "prompt.txt").read_text()
    log = [json.loads(l) for l in (conf.data_dir / "agent.log").read_text().splitlines()]
    assert any("pull_failed" in e for e in log)


def test_new_messages_carry_ids_for_reply_to(conf, fake, tmp_path):
    loop = agent_loop(conf, fake)
    m = fake.add(KIM, "@claude what's open?")
    loop.poll(now=0)
    new = (tmp_path / "prompt.txt").read_text().split("## New messages")[1]
    assert f"#{m['id']}] kim:" in new
