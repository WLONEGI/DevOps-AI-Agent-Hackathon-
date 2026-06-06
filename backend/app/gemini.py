import logging

from backend.app.config import settings
from google.genai import types

logger = logging.getLogger("server.gemini")


def create_live_connect_config(
    use_vertexai: bool,
    gcp_agent_id: str | None,
    voice_name: str | None,
    system_instruction: str | None,
    resumption_token: str | None,
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
    }

    # Set system instruction and speech/voice configurations
    if use_vertexai and gcp_agent_id:
        # Managed Agent prioritizes GCP console configuration, only override if explicitly requested
        if system_instruction is not None:
            config_params["system_instruction"] = system_instruction
        if voice_name is not None:
            config_params["speech_config"] = types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name))
            )
    else:
        # Standard Developer API configurations
        config_params["system_instruction"] = system_instruction or settings.GEMINI_SYSTEM_INSTRUCTION
        voice = voice_name or settings.GEMINI_VOICE_NAME
        config_params["speech_config"] = types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
        )

    return types.LiveConnectConfig(**config_params)
