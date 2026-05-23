import asyncio
import base64
import json
import logging
import os
import time

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
)
from google import genai
from google.genai import types

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

app = FastAPI(title="Personal Context Engine Backend API")


# Helper to initialize Gemini Client
def get_gemini_client(
    api_key: str = None,
    vertexai: bool = False,
    project: str = None,
    location: str = None,
):
    if vertexai:
        proj = project or os.getenv("GOOGLE_CLOUD_PROJECT")
        loc = location or os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        # Initialize Client for Vertex AI (Managed Agents)
        return genai.Client(vertexai=True, project=proj, location=loc)

    if api_key:
        return genai.Client(api_key=api_key)
    env_api_key = os.getenv("GOOGLE_API_KEY")
    if env_api_key:
        return genai.Client(api_key=env_api_key)
    return genai.Client()


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.websocket("/api/chat")
async def chat_endpoint(
    websocket: WebSocket,
    key: str = None,
    model: str = None,
    voice: str = None,
    instruction: str = None,
    resumption_token: str = None,
    vertexai: str = None,
    project: str = None,
    location: str = None,
    agent_id: str = None,
):
    await websocket.accept()
    logger.info("WebSocket client connected to /api/chat.")

    # Determine if we should use Vertex AI
    use_vertexai = (
        (vertexai.lower() in ("true", "1"))
        if vertexai
        else (os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() in ("true", "1"))
    )
    gcp_project = project or os.getenv("GOOGLE_CLOUD_PROJECT")
    gcp_location = location or os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    gcp_agent_id = agent_id or os.getenv("GCP_AGENT_ID")

    try:
        gemini_client = get_gemini_client(
            api_key=key,
            vertexai=use_vertexai,
            project=gcp_project,
            location=gcp_location,
        )
    except Exception as e:
        logger.error(f"Failed to initialize Gemini client: {e}")
        await websocket.close(code=1008, reason="Initialization failed: " + str(e))
        return

    # Determine the model or agent resource path to connect to
    if use_vertexai and gcp_agent_id:
        # Managed Agent Path format: projects/{project}/locations/{location}/agents/{agent_id}
        model_id = f"projects/{gcp_project}/locations/{gcp_location}/agents/{gcp_agent_id}"
        logger.info(f"Connecting to Managed Agent: {model_id}")
    else:
        model_id = model or os.getenv("GEMINI_MODEL_ID", "gemini-3.1-flash-live-preview")
        logger.info(f"Connecting to Model: {model_id}")

    voice_name = voice or os.getenv("GEMINI_VOICE_NAME", "Kore")
    system_instruction = instruction or os.getenv(
        "GEMINI_SYSTEM_INSTRUCTION",
        (
            "あなたはスマートグラスを着用したユーザーをサポートする、日本語対応の優秀なAIアシスタントです。\n"
            "画像（ユーザーの視界 of フレーム）と音声（ユーザーの質問）から、質問に対して極めて簡潔、かつわかりやすく日本語で回答してください。\n"
            "箇条書きやマークダウンの記号（*など）は音声合成の邪魔になるので一切使わずに、話し言葉の平文のみで回答してください。\n"
            "RESPOND IN JAPANESE. YOU MUST RESPOND UNMISTAKABLY IN JAPANESE."
        ),
    )

    # Configure Context Window Compression (avoids token limit issues and saves costs)
    trigger_tokens = int(os.getenv("GEMINI_COMPRESSION_TRIGGER", "25000"))
    target_tokens = int(os.getenv("GEMINI_COMPRESSION_TARGET", "8000"))
    context_compression = types.ContextWindowCompressionConfig(
        trigger_tokens=trigger_tokens,
        sliding_window=types.SlidingWindow(target_tokens=target_tokens),
    )

    # Configure Session Resumption
    # The 'transparent' parameter is only supported in Vertex AI (Managed Agent) mode,
    # and must be omitted for the standard Gemini Developer API mode.
    if use_vertexai:
        if resumption_token:
            logger.info(f"Attempting to resume session with token: {resumption_token}")
            session_resumption = types.SessionResumptionConfig(handle=resumption_token, transparent=True)
        else:
            session_resumption = types.SessionResumptionConfig(transparent=True)
    else:
        if resumption_token:
            logger.info(f"Attempting to resume session with token: {resumption_token}")
            session_resumption = types.SessionResumptionConfig(handle=resumption_token)
        else:
            session_resumption = types.SessionResumptionConfig()

    # Base configuration parameters
    config_params = {
        "response_modalities": ["AUDIO"],
        "input_audio_transcription": types.AudioTranscriptionConfig(),
        "output_audio_transcription": types.AudioTranscriptionConfig(),
        "context_window_compression": context_compression,
        "session_resumption": session_resumption,
    }

    # For Managed Agent, we prioritize GCP-side configurations (system prompt, voice).
    # We only override them if they are explicitly passed as query parameters (instruction or voice is not None).
    if use_vertexai and gcp_agent_id:
        if instruction is not None:
            config_params["system_instruction"] = instruction
        if voice is not None:
            config_params["speech_config"] = types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
            )
    else:
        # Standard configuration for developer API
        config_params["system_instruction"] = system_instruction
        config_params["speech_config"] = types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name))
        )

    config = types.LiveConnectConfig(**config_params)

    try:
        async with gemini_client.aio.live.connect(model=model_id, config=config) as session:
            logger.info("Connected to Gemini Live API WebSocket.")

            async def client_to_gemini():
                try:
                    while True:
                        start_time = time.perf_counter()
                        message = await websocket.receive_text()
                        recv_time = time.perf_counter()

                        data = json.loads(message)
                        msg_type = data.get("type")

                        if msg_type == "image":
                            img_data = data.get("data")
                            if img_data:
                                image_bytes = base64.b64decode(img_data)
                                decode_time = time.perf_counter()
                                await session.send_realtime_input(
                                    media=types.Blob(data=image_bytes, mime_type="image/jpeg")
                                )
                                send_time = time.perf_counter()
                                logger.info(
                                    f"Latency [Client -> Backend -> Gemini] for {msg_type}: "
                                    f"WS recv: {(recv_time - start_time) * 1000:.1f}ms, "
                                    f"Decode: {(decode_time - recv_time) * 1000:.1f}ms, "
                                    f"Gemini send: {(send_time - decode_time) * 1000:.1f}ms "
                                    f"(Total: {(send_time - start_time) * 1000:.1f}ms)"
                                )
                        elif msg_type == "audio":
                            aud_data = data.get("data")
                            if aud_data:
                                chunk = base64.b64decode(aud_data)
                                decode_time = time.perf_counter()
                                await session.send_realtime_input(
                                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                                )
                                send_time = time.perf_counter()
                                logger.debug(
                                    f"Latency [Client -> Backend -> Gemini] for {msg_type}: "
                                    f"WS recv: {(recv_time - start_time) * 1000:.1f}ms, "
                                    f"Decode: {(decode_time - recv_time) * 1000:.1f}ms, "
                                    f"Gemini send: {(send_time - decode_time) * 1000:.1f}ms "
                                    f"(Total: {(send_time - start_time) * 1000:.1f}ms)"
                                )
                        elif msg_type == "stop":
                            logger.info("Received stop signal. Sending audio_stream_end=True...")
                            await session.send_realtime_input(audio_stream_end=True)
                            send_time = time.perf_counter()
                            logger.info(f"Latency for stop signal: {(send_time - start_time) * 1000:.1f}ms")
                except WebSocketDisconnect:
                    logger.info("Client WebSocket disconnected in client_to_gemini loop.")
                except Exception as e:
                    logger.error(f"Error in client_to_gemini loop: {e}")
                    raise

            async def gemini_to_client():
                try:
                    async for response in session.receive():
                        recv_time = time.perf_counter()
                        if response.server_content:
                            sc = response.server_content

                            # Forward interruption event to client to clear audio playback buffers immediately
                            if sc.interrupted:
                                logger.info("User interrupted the agent response. Sending interrupt event to client.")
                                await websocket.send_json({"type": "interrupt"})

                            if sc.model_turn:
                                for part in sc.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        base64_audio = base64.b64encode(part.inline_data.data).decode("utf-8")
                                        encode_time = time.perf_counter()
                                        await websocket.send_json({"type": "audio", "data": base64_audio})
                                        send_time = time.perf_counter()
                                        logger.info(
                                            f"Latency [Gemini -> Backend -> Client] for audio chunk: "
                                            f"Encode: {(encode_time - recv_time) * 1000:.1f}ms, "
                                            f"WS send: {(send_time - encode_time) * 1000:.1f}ms "
                                            f"(Total: {(send_time - recv_time) * 1000:.1f}ms)"
                                        )

                            # Send output transcription chunk if present
                            if sc.output_transcription and sc.output_transcription.text:
                                await websocket.send_json({"type": "text", "data": sc.output_transcription.text})
                                logger.info(f"Forwarded output transcription to client: {sc.output_transcription.text}")

                            # Send user input transcription if present (optional extra client log)
                            if sc.input_transcription and sc.input_transcription.text:
                                await websocket.send_json({"type": "user_text", "data": sc.input_transcription.text})

                        # Handle and forward session resumption handle updates
                        if response.session_resumption_update:
                            sru = response.session_resumption_update
                            logger.info(
                                f"Session Resumption Update - handle: {sru.new_handle}, resumable: {sru.resumable}"
                            )
                            await websocket.send_json(
                                {
                                    "type": "resumption_token",
                                    "data": {
                                        "handle": sru.new_handle,
                                        "resumable": sru.resumable,
                                        "last_consumed_client_message_index": sru.last_consumed_client_message_index,
                                    },
                                }
                            )

                        # Handle and forward GoAway signals
                        if response.go_away:
                            logger.warning(f"Server is going away soon. Time left: {response.go_away.time_left}")
                            await websocket.send_json(
                                {"type": "go_away", "data": {"time_left": response.go_away.time_left}}
                            )
                except Exception as e:
                    logger.error(f"Error in gemini_to_client loop: {e}")
                    raise

            # Run both loops concurrently
            await asyncio.gather(client_to_gemini(), gemini_to_client())

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected.")
    except Exception as e:
        logger.error(f"WebSocket session error: {e}")
        try:
            await websocket.close(code=1011, reason=str(e))
        except:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
