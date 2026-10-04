import contextlib
import logging
from functools import cached_property
from typing import Any

from backend.app.config import settings
from google.adk.models.gemini_llm_connection import GeminiLlmConnection
from google.adk.models.google_llm import Gemini as OriginalADKGemini
from google.adk.models.llm_request import LlmRequest
from google.adk.utils.variant_utils import GoogleLLMVariant
from google.genai import Client, types
from pydantic import model_validator

logger = logging.getLogger("server.gemini")


class ADKGemini(OriginalADKGemini):
    use_vertexai_flag: bool = False
    project: str | None = None
    location: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_vertexai_flag(cls, data: Any) -> Any:
        if isinstance(data, dict) and "use_vertexai" in data and "use_vertexai_flag" not in data:
            data["use_vertexai_flag"] = data.pop("use_vertexai")
        return data

    def _init_client(self, http_options: types.HttpOptions) -> Client:
        kwargs = {
            "http_options": http_options,
            "vertexai": self.use_vertexai_flag,
        }
        if self.use_vertexai_flag:
            if self.project:
                kwargs["project"] = self.project
            if self.location:
                kwargs["location"] = self.location
        elif settings.GOOGLE_API_KEY:
            kwargs["api_key"] = settings.GOOGLE_API_KEY
        return Client(**kwargs)

    def _create_http_options(
        self, api_version: str | None = None, retry_options: Any | None = None
    ) -> types.HttpOptions:
        base_url, default_api_version = self._base_url_and_api_version
        target_api_version = default_api_version if api_version is None else api_version
        kwargs = {
            "headers": self._tracking_headers(),
            "base_url": base_url,
        }
        if target_api_version:
            kwargs["api_version"] = target_api_version
        if retry_options is not None:
            kwargs["retry_options"] = retry_options
        return types.HttpOptions(**kwargs)

    @cached_property
    def api_client(self) -> Client:
        http_options = self._create_http_options(retry_options=self.retry_options)
        return self._init_client(http_options)

    @cached_property
    def _live_api_client(self) -> Client:
        http_options = self._create_http_options(api_version=self._live_api_version)
        return self._init_client(http_options)

    @contextlib.asynccontextmanager
    async def connect(self, llm_request: LlmRequest):
        """Connects to the Gemini model and returns an llm connection."""
        live_config = llm_request.live_connect_config
        if live_config:
            if live_config.http_options:
                live_config.http_options.headers = self._merge_tracking_headers(live_config.http_options.headers or {})
                live_config.http_options.api_version = self._live_api_version

            if self.speech_config is not None:
                live_config.speech_config = self.speech_config

            # Apply config options (system_instruction and tools) if explicitly provided in request
            if llm_request.config:
                if llm_request.config.system_instruction is not None:
                    instruction = llm_request.config.system_instruction
                    if isinstance(instruction, types.Content):
                        live_config.system_instruction = instruction
                    elif isinstance(instruction, str):
                        live_config.system_instruction = types.Content(
                            role="system",
                            parts=[types.Part.from_text(text=instruction)],
                        )
                if llm_request.config.tools is not None:
                    live_config.tools = llm_request.config.tools

            if live_config.session_resumption and live_config.session_resumption.transparent:
                logger.debug(f"session resumption config: {live_config.session_resumption}")
                if self._api_backend == GoogleLLMVariant.GEMINI_API:
                    raise ValueError(
                        "Transparent session resumption is only supported for Vertex AI"
                        " backend. Please use Vertex AI backend."
                    )

        logger.info(f"Connecting to live model: {llm_request.model} with api backend: {self._api_backend}")

        async with self._live_api_client.aio.live.connect(model=llm_request.model, config=live_config) as live_session:
            yield GeminiLlmConnection(
                live_session,
                api_backend=self._api_backend,
                model_version=llm_request.model,
            )


def mask_token(token: str | None) -> str:
    """Masks sensitive token for secure logging, exposing only small prefix/suffix."""
    if not token:
        return ""
    cleaned = token.strip() if isinstance(token, str) else str(token).strip()
    if not cleaned:
        return ""
    str_token = "".join(c for c in cleaned if c.isprintable())
    if not str_token:
        return ""
    if len(str_token) <= 8:
        return "***"
    return f"{str_token[:4]}...{str_token[-4:]}"


def is_managed_agent_config(use_vertexai: bool, gcp_agent_id: str | None = None) -> bool:
    """Returns True if the session targets a Vertex AI Agent Builder Managed Agent."""
    return bool(use_vertexai and gcp_agent_id)


def create_live_connect_config(
    use_vertexai: bool,
    gcp_agent_id: str | None = None,
    voice_name: str | None = None,
    resumption_token: str | None = None,
) -> types.LiveConnectConfig:
    """Constructs the LiveConnectConfig for Gemini Live API WebSocket session."""
    # Configure Context Window Compression
    context_compression = types.ContextWindowCompressionConfig(
        trigger_tokens=settings.GEMINI_COMPRESSION_TRIGGER,
        sliding_window=types.SlidingWindow(target_tokens=settings.validated_compression_target),
    )

    # Configure Session Resumption
    resumption_params = {}
    if isinstance(resumption_token, str) and (token_handle := resumption_token.strip()):
        logger.info(
            f"Configuring {'Vertex AI' if use_vertexai else 'standard'} session resumption with token: {mask_token(token_handle)}"
        )
        resumption_params["handle"] = token_handle
    if use_vertexai:
        resumption_params["transparent"] = True
    session_resumption = types.SessionResumptionConfig(**resumption_params)

    config_params = {
        "response_modalities": ["AUDIO"],
        "input_audio_transcription": types.AudioTranscriptionConfig(),
        "output_audio_transcription": types.AudioTranscriptionConfig(),
        "context_window_compression": context_compression,
        "session_resumption": session_resumption,
        "realtime_input_config": types.RealtimeInputConfig(activity_handling="NO_INTERRUPTION"),
    }

    # Resolve voice name based on agent configuration
    if is_managed_agent_config(use_vertexai, gcp_agent_id):
        # Managed Agent prioritizes GCP console configuration, only override if explicitly requested
        voice = voice_name
    else:
        # Standard Developer API configuration uses query voice or default env voice
        voice = voice_name or settings.GEMINI_VOICE_NAME

    if voice is not None:
        config_params["speech_config"] = types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
        )

    return types.LiveConnectConfig(**config_params)
