import os
from pathlib import Path
from typing import overload

from dotenv import load_dotenv

# Load environment variables
load_dotenv()  # Default behavior, loads .env in current directory if exists

for env_path in (Path("backend/.env"), Path("/app/backend/.env")):
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)


@overload
def _get_str_env(key: str, default: str) -> str: ...


@overload
def _get_str_env(key: str, default: None = None) -> str | None: ...


def _get_str_env(key: str, default: str | None = None) -> str | None:
    val = os.getenv(key)
    if val is None:
        return default
    cleaned = val.strip()
    return cleaned if cleaned else default


TRUTHY_VALUES = frozenset({"true", "1", "yes", "on"})
FALSY_VALUES = frozenset({"false", "0", "no", "off"})


def _get_int_env(key: str, default: int, min_val: int | None = None, max_val: int | None = None) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        res = int(val.strip())
        if (min_val is not None and res < min_val) or (max_val is not None and res > max_val):
            return default
        return res
    except ValueError:
        return default


def _get_bool_env(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    cleaned = val.strip().lower()
    if not cleaned:
        return default
    if cleaned in TRUTHY_VALUES:
        return True
    if cleaned in FALSY_VALUES:
        return False
    return default


class Settings:
    GOOGLE_API_KEY: str | None = _get_str_env("GOOGLE_API_KEY")
    GEMINI_MODEL_ID: str = _get_str_env("GEMINI_MODEL_ID", "gemini-2.5-flash-native-audio-latest")
    GEMINI_VOICE_NAME: str = _get_str_env("GEMINI_VOICE_NAME", "Kore")
    GEMINI_SYSTEM_INSTRUCTION: str = _get_str_env(
        "GEMINI_SYSTEM_INSTRUCTION",
        (
            "あなたはスマートグラスを着用したユーザーをサポートする、日本語対応の優秀なAIアシスタントです。\n"
            "画像（ユーザーの視界 of フレーム）と音声（ユーザーの質問）から、質問に対して極めて簡潔、かつわかりやすく日本語で回答してください。\n"
            "箇条書きやマークダウンの記号（*など）は音声合成の邪魔になるので一切使わずに、話し言葉の平文のみで回答してください。\n"
            "RESPOND IN JAPANESE. YOU MUST RESPOND UNMISTAKABLY IN JAPANESE."
        ),
    )
    GEMINI_COMPRESSION_TRIGGER: int = _get_int_env("GEMINI_COMPRESSION_TRIGGER", 25000, min_val=1000, max_val=1_000_000)
    GEMINI_COMPRESSION_TARGET: int = _get_int_env("GEMINI_COMPRESSION_TARGET", 8000, min_val=500, max_val=500_000)

    @property
    def validated_compression_target(self) -> int:
        """Ensures compression target tokens is strictly less than compression trigger tokens."""
        if self.GEMINI_COMPRESSION_TARGET >= self.GEMINI_COMPRESSION_TRIGGER:
            return max(500, min(8000, self.GEMINI_COMPRESSION_TRIGGER // 2))
        return self.GEMINI_COMPRESSION_TARGET

    GOOGLE_GENAI_USE_VERTEXAI: bool = _get_bool_env("GOOGLE_GENAI_USE_VERTEXAI", False)
    GOOGLE_CLOUD_PROJECT: str | None = _get_str_env("GOOGLE_CLOUD_PROJECT")
    GOOGLE_CLOUD_LOCATION: str = _get_str_env("GOOGLE_CLOUD_LOCATION", "us-central1")
    GCP_AGENT_ID: str | None = _get_str_env("GCP_AGENT_ID")
    API_ACCESS_KEY: str | None = _get_str_env("API_ACCESS_KEY")
    ALLOWED_ORIGINS: str = _get_str_env("ALLOWED_ORIGINS", "*")

    @property
    def allowed_origins_set(self) -> set[str]:
        """Returns a set of normalized allowed origins parsed from ALLOWED_ORIGINS."""
        if not self.ALLOWED_ORIGINS:
            return {"*"}
        cleaned = self.ALLOWED_ORIGINS.strip()
        if not cleaned or cleaned == "*":
            return {"*"}
        parsed = {o.strip().rstrip("/").lower() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()}
        if "*" in parsed or not parsed:
            return {"*"}
        return parsed

    HOST: str = _get_str_env("HOST", "0.0.0.0")  # nosec
    PORT: int = _get_int_env("PORT", 8000, min_val=1, max_val=65535)

    MAX_PAYLOAD_SIZE: int = _get_int_env("MAX_PAYLOAD_SIZE", 5 * 1024 * 1024, min_val=1024, max_val=100 * 1024 * 1024)
    MAX_WEBSOCKET_MESSAGE_SIZE: int = _get_int_env(
        "MAX_WEBSOCKET_MESSAGE_SIZE", 10 * 1024 * 1024, min_val=1024, max_val=100 * 1024 * 1024
    )
    MAX_INSTRUCTION_LENGTH: int = _get_int_env("MAX_INSTRUCTION_LENGTH", 4096, min_val=1, max_val=65536)
    MAX_TEXT_PROMPT_LENGTH: int = _get_int_env("MAX_TEXT_PROMPT_LENGTH", 16384, min_val=1, max_val=131072)
    B64_OFFLOAD_THRESHOLD_BYTES: int = _get_int_env(
        "B64_OFFLOAD_THRESHOLD_BYTES", 65536, min_val=1024, max_val=10 * 1024 * 1024
    )

    @property
    def max_base64_payload_len(self) -> int:
        return self.MAX_PAYLOAD_SIZE * 4 // 3 + 4


settings = Settings()
