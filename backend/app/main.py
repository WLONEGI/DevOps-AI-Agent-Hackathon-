import asyncio
import base64
import json
import logging
import re
import secrets
from functools import cached_property

from backend.app.config import settings
from backend.app.gemini import create_live_connect_config
from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
)
from google.adk.models.google_llm import Gemini as OriginalADKGemini
from google.adk.models.llm_request import LlmRequest
from google.genai import Client, types


class ADKGemini(OriginalADKGemini):
    use_vertexai_flag: bool = False

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

        kwargs = {
            "http_options": types.HttpOptions(**kwargs_for_http_options),
            "vertexai": self.use_vertexai_flag,
        }
        return Client(**kwargs)

    @cached_property
    def _live_api_client(self) -> Client:
        base_url, _ = self._base_url_and_api_version
        kwargs = {
            "http_options": types.HttpOptions(
                headers=self._tracking_headers(),
                api_version=self._live_api_version,
                base_url=base_url,
            ),
            "vertexai": self.use_vertexai_flag,
        }
        return Client(**kwargs)


# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

# Validation patterns to prevent injection attacks
GCP_PROJECT_PATTERN = re.compile(r"^[a-z0-9-]{6,30}$")
GCP_LOCATION_PATTERN = re.compile(r"^[a-z0-9-]+$")
GCP_AGENT_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
GEMINI_MODEL_PATTERN = re.compile(r"^[a-zA-Z0-9.-]+$")
VOICE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")
RESUMPTION_TOKEN_PATTERN = re.compile(r"^[a-zA-Z0-9_=-]+$")

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
) -> bool:
    """Validates a parameter value against a regex pattern and check for configuration/placeholder errors.

    If validation fails, logs the error and closes the WebSocket connection.
    """
    is_placeholder = False
    if placeholder:
        if isinstance(placeholder, str):
            is_placeholder = value == placeholder
        else:
            is_placeholder = value in placeholder

    if not value or is_placeholder:
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


async def decode_and_send_blob(connection, base64_data: str, mime_type: str, msg_type: str):
    """Decodes base64 data and sends it to Gemini Live session as a Blob."""
    try:
        chunk = base64.b64decode(base64_data)
    except Exception as e:
        logger.warning(f"Failed to decode base64 {msg_type} data: {e}")
        return
    blob = types.Blob(data=chunk, mime_type=mime_type)
    await connection.send_realtime(blob)


async def run_gemini_adk_live(
    websocket: WebSocket,
    model_id: str,
    use_vertexai: bool,
    gcp_agent_id: str | None = None,
    resumption_token: str | None = None,
    voice: str | None = None,
    instruction: str | None = None,
):
    """Handles Gemini Live session (standard or Enterprise Agent Platform) streaming via WebSockets using google-adk."""
    logger.info(f"Connecting to Gemini Live session: {model_id} (VertexAI={use_vertexai})")

    # Construct LiveConnectConfig
    live_config = create_live_connect_config(
        use_vertexai=use_vertexai,
        gcp_agent_id=gcp_agent_id if use_vertexai else None,
        voice_name=voice,
        system_instruction=instruction,
        resumption_token=resumption_token,
    )

    llm_request = LlmRequest(
        model=model_id,
        live_config=live_config,
        config=types.GenerateContentConfig(
            system_instruction=instruction or settings.GEMINI_SYSTEM_INSTRUCTION,
        ),
    )

    adk_gemini = ADKGemini(model=model_id, use_vertexai_flag=use_vertexai)

    async def client_to_gemini(connection):
        try:
            # Map message types to their respective MIME types for processing
            mime_types = {
                "audio": "audio/pcm;rate=16000",
                "image": "image/jpeg",
            }
            while True:
                try:
                    message = await websocket.receive_text()
                    data = json.loads(message)

                    if not isinstance(data, dict):
                        logger.warning("Received JSON is not a dictionary.")
                        continue

                    msg_type = data.get("type")

                    if msg_type in mime_types:
                        payload = data.get("data")
                        if payload and isinstance(payload, str):
                            await decode_and_send_blob(connection, payload, mime_types[msg_type], msg_type)
                        else:
                            logger.warning(f"Payload for {msg_type} is empty or not a string.")
                    elif msg_type == "stop":
                        logger.info("Received stop signal from client. Sending audio_stream_end...")
                        await connection._gemini_session.send_realtime_input(audio_stream_end=True)
                        break
                    else:
                        logger.warning(f"Unknown message type received from client: {msg_type}")
                except json.JSONDecodeError as e:
                    logger.warning(f"Malformed JSON received from client: {e}")
                except (WebSocketDisconnect, asyncio.CancelledError):
                    raise
                except Exception as e:
                    logger.error(f"Error processing client message: {e}")
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
                            base64_audio = base64.b64encode(inline_data.data).decode("utf-8")
                            await websocket.send_json({"type": "audio", "data": base64_audio})
        except WebSocketDisconnect:
            logger.info("Client WebSocket disconnected in gemini_to_client loop.")
        except asyncio.CancelledError:
            logger.info("gemini_to_client task cancelled.")
        except Exception as e:
            logger.error(f"Error in gemini_to_client: {e}")

    try:
        async with adk_gemini.connect(llm_request) as connection:
            task_send = asyncio.create_task(client_to_gemini(connection))
            task_recv = asyncio.create_task(gemini_to_client(connection))

            try:
                await asyncio.wait({task_send, task_recv}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                task_send.cancel()
                task_recv.cancel()
    except Exception as e:
        logger.error(f"Gemini connection error: {e}")
        raise


@app.get("/health")
def health_check():
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

    # Validate voice parameter if provided
    if voice and not VOICE_PATTERN.match(voice):
        await log_and_close_websocket(
            websocket, code=1011, log_msg=f"Invalid voice format: {voice}", client_reason="Invalid voice format."
        )
        return

    # Validate resumption_token if provided
    if resumption_token and not RESUMPTION_TOKEN_PATTERN.match(resumption_token):
        await log_and_close_websocket(
            websocket,
            code=1011,
            log_msg=f"Invalid resumption token format: {resumption_token}",
            client_reason="Invalid resumption token format.",
        )
        return

    # Validate instruction parameter length if provided (limit to prevent DoS/excessive memory usage)
    if instruction and len(instruction) > 4096:
        await log_and_close_websocket(
            websocket,
            code=1011,
            log_msg="Instruction parameter exceeds maximum allowed length of 4096 characters.",
            client_reason="Instruction exceeds maximum length.",
        )
        return

    # Determine if we should use Vertex AI or standard developer Gemini Live API
    use_vertexai = settings.GOOGLE_GENAI_USE_VERTEXAI
    if vertexai is not None:
        use_vertexai = vertexai.lower() in ("true", "1")

    if use_vertexai:
        gcp_project = project or settings.GOOGLE_CLOUD_PROJECT
        gcp_location = location or settings.GOOGLE_CLOUD_LOCATION
        gcp_agent_id = agent_id or settings.GCP_AGENT_ID

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
        model_id = model or settings.GEMINI_MODEL_ID
        gcp_agent_id = None
        if not await validate_parameter(websocket, model_id, GEMINI_MODEL_PATTERN, "GEMINI_MODEL_ID"):
            return
        logger.info(f"Routing to standard developer Gemini Live API: {model_id}")

    await websocket.accept()
    logger.info("WebSocket client connected to /api/chat.")

    try:
        await run_gemini_adk_live(
            websocket=websocket,
            model_id=model_id,
            use_vertexai=use_vertexai,
            gcp_agent_id=gcp_agent_id,
            resumption_token=resumption_token,
            voice=voice,
            instruction=instruction,
        )
    except Exception as e:
        logger.error(f"Gemini Live session error: {e}")
        await safe_close_websocket(websocket, code=1011, reason=str(e))
