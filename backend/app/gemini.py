import contextlib
import logging
from functools import cached_property

from backend.app.config import settings
from google.adk.models.gemini_llm_connection import GeminiLlmConnection
from google.adk.models.google_llm import Gemini as OriginalADKGemini
from google.adk.models.llm_request import LlmRequest
from google.adk.utils.variant_utils import GoogleLLMVariant
from google.genai import Client, types

logger = logging.getLogger("server.gemini")


class ADKGemini(OriginalADKGemini):
    use_vertexai_flag: bool = False
    project: str | None = None
    location: str | None = None

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
        else:
            if settings.GOOGLE_API_KEY:
                kwargs["api_key"] = settings.GOOGLE_API_KEY
        return Client(**kwargs)

    @cached_property
    def api_client(self) -> Client:
        base_url, api_version = self._base_url_and_api_version
        kwargs_for_http_options = {
            "headers": self._tracking_headers(),
            "retry_options": self.retry_options,
            "base_url": base_url,
        }
        if api_version:
            kwargs_for_http_options["api_version"] = api_version

        return self._init_client(types.HttpOptions(**kwargs_for_http_options))

    @cached_property
    def _live_api_client(self) -> Client:
        base_url, _ = self._base_url_and_api_version
        http_options = types.HttpOptions(
            headers=self._tracking_headers(),
            api_version=self._live_api_version,
            base_url=base_url,
        )
        return self._init_client(http_options)

    @contextlib.asynccontextmanager
    async def connect(self, llm_request: LlmRequest):
        """Connects to the Gemini model and returns an llm connection."""
        if llm_request.live_connect_config and llm_request.live_connect_config.http_options:
            if not llm_request.live_connect_config.http_options.headers:
                llm_request.live_connect_config.http_options.headers = {}
            llm_request.live_connect_config.http_options.headers = self._merge_tracking_headers(
                llm_request.live_connect_config.http_options.headers
            )
            llm_request.live_connect_config.http_options.api_version = self._live_api_version

        if self.speech_config is not None:
            llm_request.live_connect_config.speech_config = self.speech_config

        # Set system_instruction only if explicitly provided in config
        if llm_request.config and llm_request.config.system_instruction is not None:
            llm_request.live_connect_config.system_instruction = types.Content(
                role="system",
                parts=[types.Part.from_text(text=llm_request.config.system_instruction)],
            )

        logger.info(f"Trying to connect to live model: {llm_request.model} with api backend: {self._api_backend}")

        if (
            llm_request.live_connect_config.session_resumption
            and llm_request.live_connect_config.session_resumption.transparent
        ):
            logger.debug(f"session resumption config: {llm_request.live_connect_config.session_resumption}")

            if self._api_backend == GoogleLLMVariant.GEMINI_API:
                raise ValueError(
                    "Transparent session resumption is only supported for Vertex AI"
                    " backend. Please use Vertex AI backend."
                )
        llm_request.live_connect_config.tools = llm_request.config.tools
        logger.debug(f"Connecting to live with llm_request:{llm_request}")
        logger.debug(f"Live connect config: {llm_request.live_connect_config}")

        async with self._live_api_client.aio.live.connect(
            model=llm_request.model, config=llm_request.live_connect_config
        ) as live_session:
            yield GeminiLlmConnection(
                live_session,
                api_backend=self._api_backend,
                model_version=llm_request.model,
            )


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
        sliding_window=types.SlidingWindow(target_tokens=settings.GEMINI_COMPRESSION_TARGET),
    )

    # Configure Session Resumption
    resumption_params = {}
    if resumption_token:
        logger.info(
            f"Configuring {'Vertex AI' if use_vertexai else 'standard'} session resumption with token: {resumption_token}"
        )
        resumption_params["handle"] = resumption_token
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

    # Set speech/voice configurations

    # Resolve voice name based on agent configuration
    is_managed_agent = use_vertexai and gcp_agent_id
    if is_managed_agent:
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
