from dataclasses import dataclass
import os
from pathlib import Path
import re
ROOT = Path(__file__).resolve().parents[1]
STORE_REPO = "catui-plataform/CATui"
BOT_REPO = "catui-plataform/catui-bot"
CORE_IDS = ("pcsx2", "eden", "rpcs3", "shadps4", "ppsspp", "cemu", "flycast")
CORE_NAMES = {"pcsx2": "PCSX2", "eden": "Eden", "rpcs3": "RPCS3", "shadps4": "shadPS4",
              "ppsspp": "PPSSPP", "cemu": "Cemu", "flycast": "Flycast"}
DEEPSEEK_MODEL = "deepseek-flash"


@dataclass(frozen=True)
class Config:
    discord_token: str
    owner_id: int
    guild_id: int
    channel_id: int
    github_token: str
    signing_key: str
    llm_key: str
    llm_model: str
    interval: int
    state_path: Path
    updates_enabled: bool

    @classmethod
    def load(cls):
        def required(name):
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"Configure {name} in the hosting environment")
            return value

        def snowflake(name):
            value = required(name)
            if not re.fullmatch(r"\d{15,22}", value):
                raise ValueError(f"{name} must be a Discord ID")
            return int(value)

        key = required("APPROVAL_SIGNING_KEY")
        if len(key) < 32:
            raise ValueError("APPROVAL_SIGNING_KEY must have at least 32 characters")
        return cls(required("DISCORD_TOKEN"), snowflake("DISCORD_OWNER_ID"),
                   snowflake("DISCORD_GUILD_ID"), snowflake("DISCORD_CHANNEL_ID"),
                   required("GITHUB_TOKEN"), key, os.environ.get("DEEPSEEK_API_KEY", "").strip(),
                   DEEPSEEK_MODEL,
                   max(300, int(os.environ.get("CHECK_INTERVAL_SECONDS", "3600"))),
                   Path(os.environ.get("STATE_PATH", "data/bot.sqlite3")),
                   os.environ.get("CORE_UPDATES_ENABLED", "true").lower() == "true")
