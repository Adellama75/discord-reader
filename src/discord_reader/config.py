import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from chatagent import Agent


@dataclass
class Config:
    token_file: Path  # bot token (chmod 600); DISCORD_TOKEN overrides
    channel: str  # the one channel id this installation watches
    label: str  # how the chat is named in prompts and logs
    data_dir: Path
    client: str = ""  # "module:Class" to use instead of discord_reader.discord.Client (same methods)
    poll: float = 15  # seconds between Discord polls
    timezone: str = "Europe/London"  # for timestamps in transcripts
    attachments_max_mb: float = 10  # attachments up to this size are saved so Claude can Read them
    names: dict[str, str] = field(default_factory=dict)  # user id -> display name override
    agent: Agent | None = None  # chat-agent settings ([agent])

    @property
    def db(self) -> Path:
        return self.data_dir / "messages.db"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"


def token(c: Config) -> str:
    t = os.environ.get("DISCORD_TOKEN", "").strip()
    if not t and c.token_file.exists():
        t = c.token_file.read_text().strip()
    if not t:
        raise SystemExit(f"no Discord token: put it in {c.token_file} (chmod 600) or set DISCORD_TOKEN")
    return t


def load(path: Path) -> Config:
    """Relative paths resolve against the config file, not the working directory, so
    `discord-reader --config /x/config.toml` (or DISCORD_READER_CONFIG) works from anywhere."""
    raw = tomllib.loads(path.read_text())
    base = path.resolve().parent

    def rel(p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else base / q

    d = raw["discord"]
    paths = raw.get("paths", {})
    return Config(
        token_file=rel(d.get("token_file", "~/.config/discord-reader/bot-token")),
        client=d.get("client", ""),
        channel=str(d["channel"]),
        label=d.get("label", str(d["channel"])),
        data_dir=rel(paths.get("data_dir", "data")),
        poll=float(d.get("poll", 15)),
        timezone=d.get("timezone", "Europe/London"),
        attachments_max_mb=float(d.get("attachments_max_mb", 10)),
        names={str(k): v for k, v in raw.get("names", {}).items()},
        agent=Agent.from_dict(raw.get("agent")),
    )
