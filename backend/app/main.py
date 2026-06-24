import asyncio
import base64
import json
import logging
import re
import secrets

from backend.app.config import settings
from backend.app.gemini import ADKGemini, create_live_connect_config
from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
)
from google.adk.models.llm_request import LlmRequest
from google.genai import types

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

# Validation patterns to prevent injection attacks
GCP_PROJECT_PATTERN = re.compile(r"^[a-z0-9-]{6,30}$")
GCP_LOCATION_PATTERN = re.compile(r"^[a-z0-9-]{1,50}$")
GCP_AGENT_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")
MODEL_PATTERN = re.compile(r"^[a-zA-Z0-9./_-]{1,200}$")
VOICE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")
RESUMPTION_TOKEN_PATTERN = re.compile(r"^[a-zA-Z0-9_=-]{1,4096}$")
VERTEXAI_PATTERN = re.compile(r"^(true|false|1|0)$", re.IGNORECASE)


app = FastAPI(title="Personal Context Engine Backend API")


async def safe_close_websocket(websocket: WebSocket, code: int, reason: str):
    """Safely closes the WebSocket connection and logs any exception at debug level."""
    try:
        # RFC 6455 limits websocket close reason to 123 bytes.
        # Sanitize and truncate to avoid triggering protocol errors.
        sanitized_reason = reason.encode("utf-8", errors="ignore")[:123].decode("utf-8", errors="ignore")
        await websocket.close(code=code, reason=sanitized_reason)
    except Exception as e:
        logger.debug(f"Failed to close websocket safely: {e}")


async def log_and_close_websocket(websocket: WebSocket, code: int, log_msg: str, client_reason: str):
    """Logs the error message and closes the WebSocket connection with the given code and reason."""
    logger.error(log_msg)
    await safe_close_websocket(websocket, code=code, reason=client_reason)


async def validate_parameter(
    websocket: WebSocket,
    value: str | None,
    pattern: re.Pattern,
    param_name: str,
    placeholder: str | tuple[str, ...] | None = None,
    required: bool = True,
) -> bool:
    """Validates a parameter value against a regex pattern and check for configuration/placeholder errors.

    If validation fails, logs the error and closes the WebSocket connection.
    """
    if not value:
        if required:
            await log_and_close_websocket(
                websocket,
                code=1011,
                log_msg=f"{param_name} is not configured. Rejecting connection.",
                client_reason=f"{param_name} is not configured.",
            )
            return False
        return True

    is_placeholder = False
    if placeholder:
        if isinstance(placeholder, str):
            is_placeholder = value == placeholder
        else:
            is_placeholder = value in placeholder

    if is_placeholder:
        await log_and_close_websocket(
            websocket,
            code=1011,
            log_msg=f"{param_name} is not configured. Rejecting connection.",
            client_reason=f"{param_name} is not configured.",
        )
        return False

    if not pattern.match(value):
        await log_and_close_websocket(
            websocket,
            code=1011,
            log_msg=f"Invalid {param_name} format: {value}",
            client_reason=f"Invalid {param_name} format.",
        )
        return False

    return True


MAX_PAYLOAD_SIZE = settings.MAX_PAYLOAD_SIZE
MIME_TYPES = {
    "audio": "audio/pcm;rate=16000",
    "image": "image/jpeg",
}


async def _safe_b64decode(data: str) -> bytes:
    """Decodes base64 data, offloading CPU-bound tasks for large payloads to a thread pool."""
    if len(data) > 65536:
        return await asyncio.to_thread(base64.b64decode, data)
    return base64.b64decode(data)


async def _safe_b64encode(data: bytes) -> str:
    """Encodes bytes to base64 string, offloading CPU-bound tasks for large payloads to a thread pool."""
    if len(data) > 65536:
        return await asyncio.to_thread(lambda d: base64.b64encode(d).decode("utf-8"), data)
    return base64.b64encode(data).decode("utf-8")


async def _send_realtime_input(connection, **kwargs) -> bool:
    """Helper to safely send realtime input to the Gemini session."""
    session = getattr(connection, "_gemini_session", None)
    if session is None:
        logger.error(f"Invalid connection: _gemini_session is not initialized. Cannot send {list(kwargs.keys())}.")
        return False
    try:
        await session.send_realtime_input(**kwargs)
        return True
    except Exception as e:
        if "audio_stream_end" in kwargs:
            logger.warning(
                f"Failed to send audio_stream_end to Gemini (session may be already responding or inactive): {e}"
            )
        else:
            logger.error(f"Error sending realtime input {list(kwargs.keys())} to Gemini: {e}")
            raise


async def decode_and_send_blob(connection, base64_data: str, mime_type: str, msg_type: str):
    """Decodes base64 data and sends it to Gemini Live session as a Blob."""
    # Prevent memory exhaustion (DoS) by checking raw base64 string length.
    if len(base64_data) > (MAX_PAYLOAD_SIZE * 4 // 3 + 4):
        logger.warning(f"Rejected base64 {msg_type} data: payload size too large.")
        return

    try:
        chunk = await _safe_b64decode(base64_data)
    except Exception as e:
        logger.warning(f"Failed to decode base64 {msg_type} data: {e}")
        return

    # Double check decoded byte size to prevent memory exhaustion
    if len(chunk) > MAX_PAYLOAD_SIZE:
        logger.warning(f"Rejected decoded {msg_type} data: payload size too large.")
        return

    blob = types.Blob(data=chunk, mime_type=mime_type)

    if msg_type == "audio":
        await _send_realtime_input(connection, audio=blob)
    elif msg_type == "image":
        await _send_realtime_input(connection, video=blob)


async def run_gemini_adk_live(
    websocket: WebSocket,
    model_id: str,
    use_vertexai: bool,
    gcp_project: str | None = None,
    gcp_location: str | None = None,
    gcp_agent_id: str | None = None,
    resumption_token: str | None = None,
    voice: str | None = None,
    instruction: str | None = None,
):
    """Handles Gemini Live session (standard or Enterprise Agent Platform) streaming via WebSockets using google-adk."""
    logger.info(f"Connecting to Gemini Live session: {model_id} (VertexAI={use_vertexai})")

    # Unify system instruction resolution.
    # For Managed Agent, we only override system instruction if instruction is explicitly requested.
    # We handle it cleanly via LlmRequest.config below, which ADKGemini.connect will map.
    is_managed_agent = use_vertexai and gcp_agent_id
    sys_instruction = instruction
    if not is_managed_agent and sys_instruction is None:
        sys_instruction = settings.GEMINI_SYSTEM_INSTRUCTION

    # Construct LiveConnectConfig
    live_config = create_live_connect_config(
        use_vertexai=use_vertexai,
        gcp_agent_id=gcp_agent_id if use_vertexai else None,
        voice_name=voice,
        resumption_token=resumption_token,
    )

    llm_request = LlmRequest(
        model=model_id,
        live_config=live_config,
        config=types.GenerateContentConfig(
            system_instruction=sys_instruction,
        ),
    )

    adk_gemini = ADKGemini(
        model=model_id,
        use_vertexai_flag=use_vertexai,
        project=gcp_project,
        location=gcp_location,
    )

    async def client_to_gemini(connection):
        try:
            while True:
                try:
                    message = await websocket.receive_text()
                    # Limit incoming message size to prevent DoS memory exhaustion
                    if len(message) > settings.MAX_WEBSOCKET_MESSAGE_SIZE:
                        logger.warning(
                            f"Received WebSocket message exceeding size limit ({settings.MAX_WEBSOCKET_MESSAGE_SIZE} bytes)."
                        )
                        await safe_close_websocket(websocket, code=1009, reason="Message too large")
                        break

                    data = json.loads(message)
                except json.JSONDecodeError as e:
                    logger.warning(f"Malformed JSON received from client: {e}")
                    continue
                except (WebSocketDisconnect, asyncio.CancelledError):
                    raise
                except Exception as e:
                    logger.error(f"Error reading client message: {e}")
                    raise

                if not isinstance(data, dict):
                    logger.warning("Received JSON is not a dictionary.")
                    continue

                msg_type = data.get("type")
                if not msg_type:
                    logger.warning("Received message with missing 'type'.")
                    continue

                try:
                    if msg_type in MIME_TYPES:
                        payload = data.get("data")
                        if payload and isinstance(payload, str):
                            await decode_and_send_blob(connection, payload, MIME_TYPES[msg_type], msg_type)
                        else:
                            logger.warning(f"Payload for {msg_type} is empty or not a string.")
                    elif msg_type == "text":
                        payload = data.get("data")
                        if payload and isinstance(payload, str):
                            logger.info(f"Received text prompt from client: {payload}")
                            await _send_realtime_input(connection, text=payload)
                        else:
                            logger.warning("Payload for text is empty or not a string.")
                    elif msg_type == "stop":
                        logger.info("Received stop signal from client. Sending audio_stream_end...")
                        await _send_realtime_input(connection, audio_stream_end=True)
                    else:
                        logger.warning(f"Unknown message type received from client: {msg_type}")
                except Exception as e:
                    logger.error(f"Gemini transmission error: {e}")
                    raise
        except WebSocketDisconnect:
            logger.info("Client WebSocket disconnected in client_to_gemini loop.")
        except asyncio.CancelledError:
            logger.info("client_to_gemini task cancelled.")
        except Exception as e:
            logger.error(f"Error in client_to_gemini: {e}")

    async def gemini_to_client(connection):
        try:
            async for response in connection.receive():
                if getattr(response, "interrupted", False):
                    logger.info("Gemini turn interrupted. Sending interrupt signal to client...")
                    await websocket.send_json({"type": "interrupt"})
                    continue

                resumption_update = getattr(response, "live_session_resumption_update", None)
                if resumption_update and getattr(resumption_update, "new_handle", None):
                    logger.info(f"Received new resumption token: {resumption_update.new_handle}")
                    await websocket.send_json(
                        {"type": "resumption_token", "data": {"handle": resumption_update.new_handle}}
                    )

                go_away = getattr(response, "go_away", None)
                if go_away:
                    time_left = str(go_away.time_left) if getattr(go_away, "time_left", None) is not None else ""
                    logger.info(f"Received GoAway. Time left: {time_left}")
                    await websocket.send_json({"type": "go_away", "data": {"time_left": time_left}})

                input_transcription = getattr(response, "input_transcription", None)
                if input_transcription and getattr(input_transcription, "text", None):
                    await websocket.send_json({"type": "user_text", "data": input_transcription.text})

                content = getattr(response, "content", None)
                if content and getattr(content, "parts", None):
                    for part in content.parts:
                        if getattr(part, "text", None):
                            await websocket.send_json({"type": "text", "data": part.text})
                        inline_data = getattr(part, "inline_data", None)
                        if inline_data and getattr(inline_data, "data", None):
                            data_bytes = inline_data.data
                            base64_audio = await _safe_b64encode(data_bytes)
                            await websocket.send_json({"type": "audio", "data": base64_audio})
        except WebSocketDisconnect:
            logger.info("Client WebSocket disconnected in gemini_to_client loop.")
        except asyncio.CancelledError:
            logger.info("gemini_to_client task cancelled.")
        except Exception as e:
            logger.error(f"Error in gemini_to_client: {e}")
            raise

    try:
        async with adk_gemini.connect(llm_request) as connection:
            task_send = asyncio.create_task(client_to_gemini(connection))
            task_recv = asyncio.create_task(gemini_to_client(connection))

            try:
                done, pending = await asyncio.wait({task_send, task_recv}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled():
                        exc = task.exception()
                        if exc:
                            raise exc
            finally:
                task_send.cancel()
                task_recv.cancel()
                await asyncio.gather(task_send, task_recv, return_exceptions=True)
    except Exception as e:
        logger.error(f"Gemini connection error: {e}")
        raise


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.websocket("/api/chat")
async def chat_endpoint(
    websocket: WebSocket,
    key: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    instruction: str | None = None,
    resumption_token: str | None = None,
    vertexai: str | None = None,
    project: str | None = None,
    location: str | None = None,
    agent_id: str | None = None,
):
    # Validate access key if configured to prevent unauthorized API use using secrets.compare_digest
    if settings.API_ACCESS_KEY and (not key or not secrets.compare_digest(key, settings.API_ACCESS_KEY)):
        await log_and_close_websocket(
            websocket,
            code=4003,
            log_msg="Unauthorized connection attempt: Invalid access key.",
            client_reason="Unauthorized: Invalid access key.",
        )
        return

    # Validate optional voice parameter if provided
    if not await validate_parameter(websocket, voice, VOICE_PATTERN, "voice", required=False):
        return

    # Validate optional resumption_token parameter if provided
    if not await validate_parameter(
        websocket, resumption_token, RESUMPTION_TOKEN_PATTERN, "resumption token", required=False
    ):
        return

    # Validate optional vertexai parameter if provided
    if not await validate_parameter(websocket, vertexai, VERTEXAI_PATTERN, "vertexai", required=False):
        return

    # Validate instruction parameter length if provided (limit to prevent DoS/excessive memory usage)
    if instruction and len(instruction) > settings.MAX_INSTRUCTION_LENGTH:
        await log_and_close_websocket(
            websocket,
            code=1011,
            log_msg=f"Instruction parameter exceeds maximum allowed length of {settings.MAX_INSTRUCTION_LENGTH} characters.",
            client_reason="Instruction exceeds maximum length.",
        )
        return

    # Determine if we should use Vertex AI or standard developer Gemini Live API
    use_vertexai = settings.GOOGLE_GENAI_USE_VERTEXAI
    if vertexai is not None:
        use_vertexai = vertexai.lower() in ("true", "1")

    gcp_project = None
    gcp_location = None

    if use_vertexai:
        gcp_project = project if project else settings.GOOGLE_CLOUD_PROJECT
        gcp_location = settings.GOOGLE_CLOUD_LOCATION

        if model and (model.startswith("publishers/") or model.startswith("gemini-")):
            model_id = model
            gcp_agent_id = None
            if not await validate_parameter(websocket, model_id, MODEL_PATTERN, "direct Vertex AI model"):
                return
            logger.info(f"Routing to direct Vertex AI model: {model_id}")
        else:
            gcp_agent_id = agent_id if agent_id else settings.GCP_AGENT_ID
            if not await validate_parameter(
                websocket, gcp_project, GCP_PROJECT_PATTERN, "GCP project", "your-gcp-project-id"
            ):
                return

            if not await validate_parameter(websocket, gcp_location, GCP_LOCATION_PATTERN, "GCP location"):
                return

            if not await validate_parameter(
                websocket, gcp_agent_id, GCP_AGENT_PATTERN, "GCP_AGENT_ID", ("YOUR_GCP_AGENT_ID", "your-agent-id")
            ):
                return

            model_id = f"projects/{gcp_project}/locations/{gcp_location}/agents/{gcp_agent_id}"
            logger.info(f"Routing to Vertex AI Agent Platform: {model_id}")
    else:
        model_id = model if model else settings.GEMINI_MODEL_ID
        gcp_agent_id = None
        if not await validate_parameter(websocket, model_id, MODEL_PATTERN, "GEMINI_MODEL_ID"):
            return
        logger.info(f"Routing to standard developer Gemini Live API: {model_id}")

    await websocket.accept()
    logger.info("WebSocket client connected to /api/chat.")

    try:
        await run_gemini_adk_live(
            websocket=websocket,
            model_id=model_id,
            use_vertexai=use_vertexai,
            gcp_project=gcp_project,
            gcp_location=gcp_location,
            gcp_agent_id=gcp_agent_id,
            resumption_token=resumption_token,
            voice=voice,
            instruction=instruction,
        )
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected normally in chat_endpoint.")
    except Exception as e:
        logger.error(f"Gemini Live session error: {e}")
        await safe_close_websocket(websocket, code=1011, reason=str(e))
