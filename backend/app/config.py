import os

from dotenv import load_dotenv

# Load environment variables
load_dotenv()
load_dotenv("backend/.env")
load_dotenv("/app/backend/.env")


def _get_int_env(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_bool_env(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


class Settings:
    GOOGLE_API_KEY: str | None = os.getenv("GOOGLE_API_KEY")
    GEMINI_MODEL_ID: str = os.getenv("GEMINI_MODEL_ID", "gemini-2.5-flash-native-audio-latest")
    GEMINI_VOICE_NAME: str = os.getenv("GEMINI_VOICE_NAME", "Kore")
    GEMINI_SYSTEM_INSTRUCTION: str = os.getenv(
        "GEMINI_SYSTEM_INSTRUCTION",
        (
            "あなたはスマートグラスを着用したユーザーをサポートする、日本語対応の優秀なAIアシスタントです。\n"
            "画像（ユーザーの視界 of フレーム）と音声（ユーザーの質問）から、質問に対して極めて簡潔、かつわかりやすく日本語で回答してください。\n"
            "箇条書きやマークダウンの記号（*など）は音声合成の邪魔になるので一切使わずに、話し言葉の平文のみで回答してください。\n"
            "RESPOND IN JAPANESE. YOU MUST RESPOND UNMISTAKABLY IN JAPANESE."
        ),
    )
    GEMINI_COMPRESSION_TRIGGER: int = _get_int_env("GEMINI_COMPRESSION_TRIGGER", 25000)
    GEMINI_COMPRESSION_TARGET: int = _get_int_env("GEMINI_COMPRESSION_TARGET", 8000)

    GOOGLE_GENAI_USE_VERTEXAI: bool = _get_bool_env("GOOGLE_GENAI_USE_VERTEXAI", False)
    GOOGLE_CLOUD_PROJECT: str | None = os.getenv("GOOGLE_CLOUD_PROJECT")
    GOOGLE_CLOUD_LOCATION: str = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    GCP_AGENT_ID: str | None = os.getenv("GCP_AGENT_ID")
    API_ACCESS_KEY: str | None = os.getenv("API_ACCESS_KEY")
    MAX_PAYLOAD_SIZE: int = _get_int_env("MAX_PAYLOAD_SIZE", 5 * 1024 * 1024)
    MAX_WEBSOCKET_MESSAGE_SIZE: int = _get_int_env("MAX_WEBSOCKET_MESSAGE_SIZE", 10 * 1024 * 1024)
    MAX_INSTRUCTION_LENGTH: int = _get_int_env("MAX_INSTRUCTION_LENGTH", 4096)


settings = Settings()
