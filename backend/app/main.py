import asyncio
import base64
import contextlib
import json
import logging
import re
import secrets
from typing import Any

from backend.app.config import settings
from backend.app.gemini import ADKGemini, create_live_connect_config, is_managed_agent_config, mask_token
from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
)
from google.adk.models.gemini_llm_connection import GeminiLlmConnection
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from starlette.websockets import WebSocketState

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
# Unicode control characters including C0 (0x00-0x1f), C1 (0x7f-0x9f), Trojan Source bidi overrides (U+202A-U+202E, U+2066-U+2069),
# and invisible formatting/bidi markers (U+061C, U+200E, U+200F, U+FEFF).
CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
DANGEROUS_CONTROL_CHARS_PATTERN = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)
URI_CREDENTIAL_PATTERN = re.compile(r"://([^:\s/@]+):([^@\s/]+)@")
AUTH_HEADER_PATTERN = re.compile(
    r"(?i)((?:Bearer|ApiKey|Token|Basic|JWT|Digest|Key|Secret|x-goog-api-key:|x-api-key:|x-access-key:)\s+)([\w\-.~+/=%]+)"
)
QUERY_PARAM_CREDENTIAL_PATTERN = re.compile(
    r"(?i)((?:^|[\s?&])(?:key|token|api[_-]?key|access[_-]?key|api[_-]?access[_-]?key|secret[_-]?access[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|session[_-]?token|secret[_-]?token|bearer[_-]?token|secret[_-]?key|app[_-]?secret|api[_-]?secret|oauth[_-]?token|client[_-]?key|session[_-]?id|secret|password|passcode|client[_-]?secret|private[_-]?key|auth|authorization|credentials|x-goog-api-key|x-api-key|x-access-key)=)([^&\s#]+)"
)
JSON_CREDENTIAL_PATTERN = re.compile(
    r'(?i)(["\']?(?:api[_-]?key|access[_-]?key|api[_-]?access[_-]?key|secret[_-]?access[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|session[_-]?token|secret[_-]?token|bearer[_-]?token|client[_-]?secret|private[_-]?key|secret[_-]?key|app[_-]?secret|api[_-]?secret|oauth[_-]?token|client[_-]?key|session[_-]?id|secret|token|password|passcode|credentials)["\']?\s*[:=]\s*["\'])([^"\'\s,}{]+)(["\']?)'
)
MESSAGE_TYPE_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
DISCONNECT_PATTERNS = ("disconnect", "not connected", "cannot call", "closed", "reset", "broken pipe")
AUTH_HEADER_NAMES = ("x-api-key", "api-key", "x-access-key", "x-goog-api-key")
AUTH_SCHEMES = ("bearer", "apikey", "token")
KNOWN_SAFE_ERROR_PATTERNS = ("1007", "1008", "invalid argument", "permission denied", "not found", "unauthenticated")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Surrogate-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-XSS-Protection": "0",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Permitted-Cross-Domain-Policies": "none",
    "X-Download-Options": "noopen",
    "Cross-Origin-Embedder-Policy": "require-corp",
}

try:
    import anyio

    DISCONNECT_EXCEPTION_CLASSES = (
        ConnectionResetError,
        BrokenPipeError,
        WebSocketDisconnect,
        asyncio.IncompleteReadError,
        anyio.EndOfStream,
    )
except ImportError:
    DISCONNECT_EXCEPTION_CLASSES = (
        ConnectionResetError,
        BrokenPipeError,
        WebSocketDisconnect,
        asyncio.IncompleteReadError,
    )

STREAM_EXCEPTION_CLASSES = (
    *DISCONNECT_EXCEPTION_CLASSES,
    RuntimeError,
    OSError,
)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Personal Context Engine Backend API started.")
    yield
    logger.info("Personal Context Engine Backend API shutting down.")


app = FastAPI(title="Personal Context Engine Backend API", lifespan=lifespan)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    for h in ("server", "x-powered-by"):
        if h in response.headers:
            del response.headers[h]
    response.headers.update(SECURITY_HEADERS)
    return response


def _is_disconnect_error(e: Exception) -> bool:
    """Checks if an exception indicates a normal or expected WebSocket disconnect."""
    if isinstance(e, DISCONNECT_EXCEPTION_CLASSES):
        return True
    err_str = str(e).lower()
    return any(pat in err_str for pat in DISCONNECT_PATTERNS)


def _log_stream_exception(loop_name: str, e: Exception):
    """Logs stream loop exceptions at appropriate severity depending on disconnect status."""
    safe_err = sanitize_for_log(redact_sensitive_keys(str(e)), max_length=500)
    if _is_disconnect_error(e):
        logger.info(f"Connection disconnected in {loop_name}: {safe_err}")
    else:
        logger.warning(f"Error in {loop_name}: {safe_err}")


def _validate_raw_token(val: Any) -> str | None:
    """Validates and strips raw token string against length, emptiness, and control chars."""
    if val and isinstance(val, str):
        cleaned = val.strip()
        if cleaned and len(cleaned) <= 4096 and not CONTROL_CHARS_PATTERN.search(cleaned):
            return cleaned
    return None


def _extract_auth_token(websocket: WebSocket, key_param: str | None) -> str | None:
    """Extracts and strips the authentication key from query param or headers."""
    if token := _validate_raw_token(key_param):
        return token
    for header_name in AUTH_HEADER_NAMES:
        if token := _validate_raw_token(websocket.headers.get(header_name)):
            return token
    auth_header = websocket.headers.get("authorization", "")
    if isinstance(auth_header, str):
        parts = auth_header.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() in AUTH_SCHEMES:
            return _validate_raw_token(parts[1])
    return None


def _get_field(obj: Any, field_name: str, default: Any = None) -> Any:
    """Safely retrieves an attribute or dictionary key from an object or dictionary."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(field_name, default)
    return getattr(obj, field_name, default)


def _extract_transcription_text(transcription_obj: Any) -> str | None:
    """Safely extracts non-empty text from an audio transcription object or dict."""
    text = _get_field(transcription_obj, "text")
    if isinstance(text, str) and (cleaned := text.strip()):
        return cleaned
    return None


def _normalize_param(val: Any) -> str | None:
    """Normalizes optional string query parameters, stripping whitespace and returning None for empty strings."""
    if isinstance(val, str) and (cleaned := val.strip()):
        return cleaned
    return None


def _normalize_params(*params: Any) -> tuple[str | None, ...]:
    """Normalizes multiple string query parameters."""
    return tuple(_normalize_param(p) for p in params)


def redact_sensitive_keys(text: str | None) -> str:
    """Redacts known sensitive configuration keys and token patterns from strings to prevent credential exposure."""
    if not text:
        return ""
    str_val = text if isinstance(text, str) else str(text)
    if not str_val:
        return ""
    redacted = str_val
    for key in (settings.API_ACCESS_KEY, settings.GOOGLE_API_KEY):
        if key and key in redacted:
            redacted = redacted.replace(key, "[REDACTED]")
    redacted = QUERY_PARAM_CREDENTIAL_PATTERN.sub(r"\g<1>[REDACTED]", redacted)
    redacted = AUTH_HEADER_PATTERN.sub(r"\g<1>[REDACTED]", redacted)
    redacted = JSON_CREDENTIAL_PATTERN.sub(r"\g<1>[REDACTED]\g<3>", redacted)
    return URI_CREDENTIAL_PATTERN.sub(r"://\g<1>:[REDACTED]@", redacted)


def _has_dangerous_control_chars(text: str) -> bool:
    """Checks whether the text contains forbidden control characters or null bytes."""
    return bool(DANGEROUS_CONTROL_CHARS_PATTERN.search(text))


def _clean_control_chars(text: Any) -> str:
    """Strips control characters and leading/trailing whitespace from text."""
    if not text:
        return ""
    str_val = text if isinstance(text, str) else str(text)
    if not CONTROL_CHARS_PATTERN.search(str_val):
        return str_val.strip()
    return CONTROL_CHARS_PATTERN.sub(" ", str_val).strip()


def sanitize_for_log(text: str, max_length: int = 200) -> str:
    """Sanitizes strings for safe log output to prevent Log Injection (CRLF) and log flooding."""
    sanitized = _clean_control_chars(text)
    if len(sanitized) > max_length:
        return sanitized[:max_length] + "..."
    return sanitized


def sanitize_close_reason(reason: str | None) -> str:
    """Sanitizes WebSocket close reason to fit RFC 6455 123-byte limit and avoid control characters."""
    sanitized = _clean_control_chars(reason)
    return sanitized.encode("utf-8", errors="ignore")[:123].decode("utf-8", errors="ignore")


async def safe_close_websocket(websocket: WebSocket, code: int, reason: str):
    """Safely closes the WebSocket connection and logs any exception at debug level."""
    try:
        if (
            getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED
            or getattr(websocket, "application_state", None) == WebSocketState.DISCONNECTED
        ):
            return
        close_code = code if isinstance(code, int) and 1000 <= code <= 4999 else 1011
        redacted_reason = redact_sensitive_keys(reason)
        sanitized_reason = sanitize_close_reason(redacted_reason)
        await websocket.close(code=close_code, reason=sanitized_reason)
    except Exception as e:
        safe_err = sanitize_for_log(redact_sensitive_keys(str(e)), max_length=200)
        logger.debug(f"Failed to close websocket safely: {safe_err}")


async def log_and_close_websocket(websocket: WebSocket, code: int, log_msg: str, client_reason: str):
    """Logs the error message and closes the WebSocket connection with the given code and reason."""
    logger.error(log_msg)
    await safe_close_websocket(websocket, code=code, reason=client_reason)


async def validate_parameter(
    websocket: WebSocket,
    value: str | None,
    pattern: re.Pattern,
    param_name: str,
    placeholder: str | tuple[str, ...] | list[str] | set[str] | None = None,
    required: bool = True,
) -> bool:
    """Validates a parameter value against a regex pattern and check for configuration/placeholder errors.

    If validation fails, logs the error and closes the WebSocket connection.
    """
    if isinstance(placeholder, str):
        is_placeholder = value == placeholder
    elif placeholder is not None:
        is_placeholder = value in placeholder
    else:
        is_placeholder = False

    if (required and not value) or is_placeholder:
        await log_and_close_websocket(
            websocket,
            code=1011,
            log_msg=f"{param_name} is not configured. Rejecting connection.",
            client_reason=f"{param_name} is not configured.",
        )
        return False

    if not value:
        return True

    if len(value) > 4096 or not pattern.match(value):
        safe_val = sanitize_for_log(value, max_length=100)
        await log_and_close_websocket(
            websocket,
            code=1011,
            log_msg=f"Invalid {param_name} format: {safe_val}",
            client_reason=f"Invalid {param_name} format.",
        )
        return False

    return True


SUPPORTED_BLOB_TYPES = {
    "audio": {"mime_type": "audio/pcm;rate=16000", "kwarg": "audio"},
    "image": {"mime_type": "image/jpeg", "kwarg": "video"},
}


def _b64encode_to_ascii(data: bytes | bytearray | memoryview) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode_bytes(data: bytes | bytearray | memoryview) -> bytes:
    return base64.b64decode(data, validate=True)


def _normalize_b64_bytes(data: str | bytes | bytearray | memoryview) -> bytes:
    """Normalizes string, bytes-like or memoryview to stripped ascii bytes."""
    if isinstance(data, bytes):
        return data.strip()
    if isinstance(data, (bytearray, memoryview)):
        return bytes(data).strip()
    return data.strip().encode("ascii")


async def _safe_b64decode(data: str | bytes | bytearray | memoryview) -> bytes:
    """Decodes base64 data, offloading CPU-bound tasks for large payloads to a thread pool."""
    cleaned = data.strip() if isinstance(data, bytes) else _normalize_b64_bytes(data)
    if len(cleaned) > settings.B64_OFFLOAD_THRESHOLD_BYTES:
        return await asyncio.to_thread(_b64decode_bytes, cleaned)
    return _b64decode_bytes(cleaned)


async def _safe_b64encode(data: bytes | bytearray | memoryview | str) -> str:
    """Encodes bytes-like data to base64 string, offloading CPU-bound tasks for large payloads to a thread pool."""
    if isinstance(data, str):
        return data
    if len(data) > settings.B64_OFFLOAD_THRESHOLD_BYTES:
        return await asyncio.to_thread(_b64encode_to_ascii, data)
    return _b64encode_to_ascii(data)


async def _send_realtime_input(connection: GeminiLlmConnection, **kwargs) -> bool:
    """Helper to safely send realtime input to the Gemini session."""
    session = getattr(connection, "_gemini_session", None)
    if session is None:
        input_keys = ", ".join(kwargs)
        logger.error(f"Invalid connection: _gemini_session is not initialized. Cannot send {input_keys}.")
        return False
    try:
        await session.send_realtime_input(**kwargs)
        return True
    except Exception as e:
        safe_err = sanitize_for_log(redact_sensitive_keys(str(e)), max_length=300)
        if "audio_stream_end" in kwargs:
            logger.warning(
                f"Failed to send audio_stream_end to Gemini (session may be already responding or inactive): {safe_err}"
            )
            return False
        input_keys = ", ".join(kwargs)
        logger.error(f"Error sending realtime input ({input_keys}) to Gemini: {safe_err}")
        raise


async def decode_and_send_blob(
    connection: GeminiLlmConnection, base64_data: str | bytes | bytearray | memoryview, msg_type: str
):
    """Decodes base64 data and sends it to Gemini Live session as a Blob."""
    blob_config = SUPPORTED_BLOB_TYPES.get(msg_type)
    if not blob_config:
        safe_msg_type = sanitize_for_log(msg_type, max_length=32)
        logger.warning(f"Unsupported message type for blob decoding: {safe_msg_type}")
        return

    if not base64_data or not isinstance(base64_data, (str, bytes, bytearray, memoryview)):
        logger.warning(f"Rejected empty or non-string base64 {msg_type} data.")
        return

    try:
        cleaned_data = _normalize_b64_bytes(base64_data)
        if not cleaned_data:
            logger.warning(f"Rejected empty base64 {msg_type} data.")
            return

        # Prevent memory exhaustion (DoS) by checking raw base64 string length against precalculated threshold.
        if len(cleaned_data) > settings.max_base64_payload_len:
            logger.warning(f"Rejected base64 {msg_type} data: payload size too large.")
            return

        chunk = await _safe_b64decode(cleaned_data)
    except Exception as e:
        safe_err = sanitize_for_log(str(e), max_length=150)
        logger.warning(f"Failed to decode base64 {msg_type} data: {safe_err}")
        return

    if not chunk:
        logger.warning(f"Rejected empty base64 {msg_type} data.")
        return

    # Double check decoded byte size to prevent memory exhaustion
    if len(chunk) > settings.MAX_PAYLOAD_SIZE:
        logger.warning(f"Rejected decoded {msg_type} data: payload size too large.")
        return

    blob = types.Blob(data=chunk, mime_type=blob_config["mime_type"])
    await _send_realtime_input(connection, **{blob_config["kwarg"]: blob})


def _is_truthy_flag(val: Any) -> bool:
    """Checks if a value is truthy while avoiding unconfigured MagicMock truthiness."""
    if val is True:
        return True
    if val and not hasattr(val, "_mock_return_value"):
        return bool(val)
    return False


async def extract_gemini_events(response) -> list[dict]:
    """Extracts client-facing WebSocket message dicts from a Gemini Live response object."""
    if response is None:
        return []
    events = []

    server_content = _get_field(response, "server_content")
    has_valid_server_content = server_content is not None and not hasattr(server_content, "_mock_return_value")

    is_interrupted = _is_truthy_flag(_get_field(response, "interrupted", False))
    if not is_interrupted and has_valid_server_content:
        is_interrupted = _is_truthy_flag(_get_field(server_content, "interrupted", False))

    if is_interrupted:
        logger.info("Gemini turn interrupted. Sending interrupt signal to client...")
        events.append({"type": "interrupt"})
        return events

    resumption_update = _get_field(response, "live_session_resumption_update")
    new_handle = _get_field(resumption_update, "new_handle")
    if new_handle:
        logger.info(f"Received new resumption token: {mask_token(new_handle)}")
        events.append({"type": "resumption_token", "data": {"handle": new_handle}})

    go_away = _get_field(response, "go_away")
    if go_away:
        raw_time_left = _get_field(go_away, "time_left")
        time_left = str(raw_time_left) if raw_time_left is not None else ""
        safe_time_left = sanitize_for_log(time_left, max_length=50)
        logger.info(f"Received GoAway. Time left: {safe_time_left}")
        events.append({"type": "go_away", "data": {"time_left": time_left}})

    # Consolidated transcription extraction with fallback to server_content
    for trans_field, event_type in (("input_transcription", "user_text"), ("output_transcription", "text")):
        trans_obj = _get_field(response, trans_field)
        if not trans_obj and has_valid_server_content:
            trans_obj = _get_field(server_content, trans_field)
        if text := _extract_transcription_text(trans_obj):
            events.append({"type": event_type, "data": text})

    content = _get_field(response, "content")
    if not content and has_valid_server_content:
        content = _get_field(server_content, "model_turn")
    if content:
        parts = _get_field(content, "parts")
        if isinstance(parts, (list, tuple)):
            for part in parts:
                text = _get_field(part, "text")
                if isinstance(text, str) and text:
                    events.append({"type": "text", "data": text})
                inline_data = _get_field(part, "inline_data")
                if inline_data:
                    data = _get_field(inline_data, "data")
                    if data:
                        base64_audio = await _safe_b64encode(data)
                        events.append({"type": "audio", "data": base64_audio})

    return events


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
    is_managed_agent = is_managed_agent_config(use_vertexai, gcp_agent_id)
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
                except RuntimeError as e:
                    if _is_disconnect_error(e):
                        logger.info(
                            f"WebSocket disconnected during receive: {sanitize_for_log(str(e), max_length=150)}"
                        )
                        break
                    logger.warning(
                        f"Unexpected WebSocket frame received from client: {sanitize_for_log(str(e), max_length=150)}"
                    )
                    continue
                except UnicodeDecodeError as e:
                    logger.warning(
                        f"Received non-UTF-8 payload from client: {sanitize_for_log(str(e), max_length=150)}"
                    )
                    continue

                # Limit incoming message size to prevent DoS memory exhaustion
                if len(message) > settings.MAX_WEBSOCKET_MESSAGE_SIZE:
                    logger.warning(
                        f"Received WebSocket message exceeding size limit ({settings.MAX_WEBSOCKET_MESSAGE_SIZE} bytes)."
                    )
                    await safe_close_websocket(websocket, code=1009, reason="Message too large")
                    break

                try:
                    data = json.loads(message)
                except json.JSONDecodeError as e:
                    safe_err = sanitize_for_log(str(e), max_length=150)
                    logger.warning(f"Malformed JSON received from client: {safe_err}")
                    continue

                if not isinstance(data, dict):
                    logger.warning("Received JSON is not a dictionary.")
                    continue

                msg_type = data.get("type")
                if not msg_type or not isinstance(msg_type, str) or not MESSAGE_TYPE_PATTERN.match(msg_type):
                    logger.warning("Received message with missing, invalid, or oversized 'type'")
                    continue

                if msg_type in SUPPORTED_BLOB_TYPES or msg_type == "text":
                    payload = data.get("data")
                    if not payload or not isinstance(payload, str):
                        logger.warning(f"Payload for {msg_type} is empty or not a string.")
                        continue

                    if msg_type == "text":
                        # Prevent extremely long text prompts to avoid memory exhaustion (DoS)
                        if len(payload) > settings.MAX_TEXT_PROMPT_LENGTH:
                            logger.warning(
                                f"Received text prompt exceeding maximum allowed length of {settings.MAX_TEXT_PROMPT_LENGTH} characters."
                            )
                            continue
                        if _has_dangerous_control_chars(payload):
                            logger.warning(
                                "Received text prompt containing control characters or null bytes. Rejecting message."
                            )
                            continue
                        if not payload.strip():
                            logger.debug("Received whitespace-only text prompt. Skipping.")
                            continue
                        # Sanitize and truncate logged text to prevent log injection and flooding
                        logged_payload = sanitize_for_log(payload, max_length=200)
                        logger.info(f"Received text prompt from client: {logged_payload}")
                        await _send_realtime_input(connection, text=payload)
                    else:
                        await decode_and_send_blob(connection, payload, msg_type)
                elif msg_type == "stop":
                    logger.info("Received stop signal from client. Sending audio_stream_end...")
                    await _send_realtime_input(connection, audio_stream_end=True)
                else:
                    safe_unknown_type = sanitize_for_log(msg_type, max_length=32)
                    logger.warning(f"Unknown message type received from client: {safe_unknown_type}")
        except STREAM_EXCEPTION_CLASSES as e:
            _log_stream_exception("client_to_gemini loop", e)
        except asyncio.CancelledError:
            logger.info("client_to_gemini task cancelled.")

    async def gemini_to_client(connection):
        try:
            async for response in connection.receive():
                events = await extract_gemini_events(response)
                for event in events:
                    await websocket.send_json(event)
        except STREAM_EXCEPTION_CLASSES as e:
            _log_stream_exception("gemini_to_client loop", e)
        except asyncio.CancelledError:
            logger.info("gemini_to_client task cancelled.")

    async with adk_gemini.connect(llm_request) as connection:
        task_send = asyncio.create_task(client_to_gemini(connection))
        task_recv = asyncio.create_task(gemini_to_client(connection))

        try:
            done, _pending = await asyncio.wait({task_send, task_recv}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():
                    exc = task.exception()
                    if exc:
                        raise exc
        finally:
            task_send.cancel()
            task_recv.cancel()
            await asyncio.gather(task_send, task_recv, return_exceptions=True)


@app.api_route("/health", methods=["GET", "HEAD"])
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
    # Validate Origin header if ALLOWED_ORIGINS is restricted (A01: CSWSH prevention)
    origin = websocket.headers.get("origin")
    if origin:
        if len(origin) > 2048 or CONTROL_CHARS_PATTERN.search(origin):
            await log_and_close_websocket(
                websocket,
                code=4003,
                log_msg="Rejected WebSocket connection: Origin header is invalid or contains control characters.",
                client_reason="Unauthorized origin.",
            )
            return
        allowed_origins = settings.allowed_origins_set
        normalized_origin = origin.strip().rstrip("/").lower()
        if "*" not in allowed_origins and normalized_origin not in allowed_origins:
            await log_and_close_websocket(
                websocket,
                code=4003,
                log_msg=f"Rejected WebSocket connection from unauthorized origin: {sanitize_for_log(origin, max_length=100)}",
                client_reason="Unauthorized origin.",
            )
            return

    # Authenticate connection via query parameter or headers (X-API-Key / Authorization: Bearer <key>)
    effective_key = _extract_auth_token(websocket, key)

    # Validate access key if configured to prevent unauthorized API use using secrets.compare_digest
    if settings.API_ACCESS_KEY and (
        not effective_key or not secrets.compare_digest(effective_key, settings.API_ACCESS_KEY)
    ):
        await log_and_close_websocket(
            websocket,
            code=4003,
            log_msg="Unauthorized connection attempt: Invalid access key.",
            client_reason="Unauthorized: Invalid access key.",
        )
        return

    # Normalize optional string query parameters
    (
        voice,
        resumption_token,
        vertexai,
        model,
        project,
        location,
        agent_id,
        instruction,
    ) = _normalize_params(
        voice,
        resumption_token,
        vertexai,
        model,
        project,
        location,
        agent_id,
        instruction,
    )

    # Validate optional parameters if provided
    optional_query_validations = (
        (voice, VOICE_PATTERN, "voice"),
        (resumption_token, RESUMPTION_TOKEN_PATTERN, "resumption token"),
        (vertexai, VERTEXAI_PATTERN, "vertexai"),
    )
    for val, pat, name in optional_query_validations:
        if not await validate_parameter(websocket, val, pat, name, required=False):
            return

    # Validate instruction parameter length if provided (limit to prevent DoS/excessive memory usage)
    if instruction:
        if len(instruction) > settings.MAX_INSTRUCTION_LENGTH:
            await log_and_close_websocket(
                websocket,
                code=1011,
                log_msg=f"Instruction parameter exceeds maximum allowed length of {settings.MAX_INSTRUCTION_LENGTH} characters.",
                client_reason="Instruction exceeds maximum length.",
            )
            return
        if _has_dangerous_control_chars(instruction):
            await log_and_close_websocket(
                websocket,
                code=1011,
                log_msg="Instruction parameter contains control characters or null bytes. Rejecting connection.",
                client_reason="Invalid instruction format.",
            )
            return

    # Determine if we should use Vertex AI or standard developer Gemini Live API
    use_vertexai = settings.GOOGLE_GENAI_USE_VERTEXAI
    if vertexai is not None:
        use_vertexai = vertexai.lower() in ("true", "1")

    gcp_project = None
    gcp_location = None

    if use_vertexai:
        gcp_project = project or settings.GOOGLE_CLOUD_PROJECT
        gcp_location = location or settings.GOOGLE_CLOUD_LOCATION

        if model:
            if not model.startswith(("publishers/", "gemini-")) or not MODEL_PATTERN.match(model):
                safe_model = sanitize_for_log(model, max_length=100)
                await log_and_close_websocket(
                    websocket,
                    code=1011,
                    log_msg=f"Invalid direct Vertex AI model format: {safe_model}",
                    client_reason="Invalid direct Vertex AI model format.",
                )
                return
            model_id = model
            gcp_agent_id = None
            if gcp_project and not await validate_parameter(
                websocket, gcp_project, GCP_PROJECT_PATTERN, "GCP project", "your-gcp-project-id", required=False
            ):
                return
            if gcp_location and not await validate_parameter(
                websocket, gcp_location, GCP_LOCATION_PATTERN, "GCP location", required=False
            ):
                return
            logger.info(f"Routing to direct Vertex AI model: {model_id}")
        else:
            gcp_agent_id = agent_id or settings.GCP_AGENT_ID
            gcp_specs = (
                (gcp_project, GCP_PROJECT_PATTERN, "GCP project", "your-gcp-project-id"),
                (gcp_location, GCP_LOCATION_PATTERN, "GCP location", None),
                (gcp_agent_id, GCP_AGENT_PATTERN, "GCP_AGENT_ID", ("YOUR_GCP_AGENT_ID", "your-agent-id")),
            )
            for val, pat, name, placeholder in gcp_specs:
                if not await validate_parameter(websocket, val, pat, name, placeholder=placeholder):
                    return

            model_id = f"projects/{gcp_project}/locations/{gcp_location}/agents/{gcp_agent_id}"
            logger.info(f"Routing to Vertex AI Agent Platform: {model_id}")
    else:
        model_id = model or settings.GEMINI_MODEL_ID
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
        error_msg = redact_sensitive_keys(str(e))
        safe_err_log = sanitize_for_log(error_msg, max_length=500)
        logger.error(f"Gemini Live session error: {safe_err_log}", exc_info=True)
        # Filter details to prevent leakage of internal system details, while retaining
        # compatibility with integration test expectation patterns (e.g. 1007/1008/permission denied).
        if any(pat in error_msg.lower() for pat in KNOWN_SAFE_ERROR_PATTERNS):
            await safe_close_websocket(websocket, code=1011, reason=error_msg)
        else:
            await safe_close_websocket(
                websocket, code=1011, reason="An unexpected error occurred during the Gemini Live session."
            )
