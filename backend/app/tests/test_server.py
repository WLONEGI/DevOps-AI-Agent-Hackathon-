import asyncio
import base64
import binascii
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from backend.app.config import _get_bool_env, _get_int_env, _get_str_env, settings
from backend.app.gemini import ADKGemini, create_live_connect_config, is_managed_agent_config, mask_token
from google.genai import types
from starlette.websockets import WebSocketDisconnect, WebSocketState

# Force default test configurations to isolate tests from local .env settings
settings.GOOGLE_GENAI_USE_VERTEXAI = False
settings.GCP_AGENT_ID = None

from backend.app.main import (
    AUTH_HEADER_NAMES,
    STREAM_EXCEPTION_CLASSES,
    _b64decode_bytes,
    _clean_control_chars,
    _extract_auth_token,
    _extract_transcription_text,
    _get_field,
    _has_dangerous_control_chars,
    _is_disconnect_error,
    _is_truthy_flag,
    _log_stream_exception,
    _normalize_b64_bytes,
    _normalize_param,
    _normalize_params,
    _safe_b64decode,
    _safe_b64encode,
    _send_realtime_input,
    _validate_raw_token,
    app,
    decode_and_send_blob,
    extract_gemini_events,
    redact_sensitive_keys,
    safe_close_websocket,
    sanitize_close_reason,
    sanitize_for_log,
    validate_parameter,
)
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health_check():
    # 1. Health Check Test
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_websocket_chat_missing_config():
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect("/api/chat") as ws:
        ws.receive_text()
    assert excinfo.value.code == 1011


def test_websocket_chat_unauthorized():
    # Enable API_ACCESS_KEY for this test
    settings.API_ACCESS_KEY = "super_secret_key"
    try:
        # 1. Test connection with missing or incorrect key
        with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect("/api/chat?key=wrong_key") as ws:
            ws.receive_text()
        assert excinfo.value.code == 4003

        # 2. Test connection with correct key
        # (It will pass authentication, then raise 1011 due to missing model/project config)
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/chat?key=super_secret_key") as ws,
        ):
            ws.receive_text()
        assert excinfo.value.code == 1011
    finally:
        # Restore configuration
        settings.API_ACCESS_KEY = None


class AsyncContextManagerMock:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        pass


def setup_mock_gemini_connection(mock_gemini_class):
    mock_gemini = MagicMock()
    mock_gemini_class.return_value = mock_gemini

    mock_connection = MagicMock()
    mock_gemini.connect.return_value = AsyncContextManagerMock(mock_connection)

    # Mock underlying _gemini_session
    mock_session = MagicMock()

    async def mock_send_realtime_input(*args, **kwargs):
        pass

    mock_session.send_realtime_input = MagicMock(side_effect=mock_send_realtime_input)
    mock_connection._gemini_session = mock_session

    # Mock LlmResponse objects
    mock_resp1 = MagicMock()
    mock_resp1.interrupted = False
    mock_resp1.server_content = None
    mock_resp1.live_session_resumption_update = None
    mock_resp1.go_away = None
    mock_resp1.input_transcription = MagicMock(text="ユーザー発話")
    mock_resp1.content = None

    mock_resp2 = MagicMock()
    mock_resp2.interrupted = False
    mock_resp2.server_content = None
    mock_resp2.live_session_resumption_update = None
    mock_resp2.go_away = None
    mock_resp2.input_transcription = None

    part1 = MagicMock()
    part1.text = "テスト応答"
    part1.inline_data = None

    part2 = MagicMock()
    part2.text = None
    part2.inline_data = MagicMock(data=b"fakeaudio")

    mock_resp2.content = MagicMock(parts=[part1, part2])

    async def mock_receive():
        yield mock_resp1
        yield mock_resp2
        # Keep loop running for a bit to process client messages
        await asyncio.sleep(0.2)

    mock_connection.receive = MagicMock(side_effect=mock_receive)
    return mock_connection


def _verify_standard_responses(ws):
    """Helper to verify that we receive audio, text, and user_text responses."""
    received_types = []
    for _ in range(3):
        resp = ws.receive_json()
        received_types.append(resp.get("type"))

    assert "audio" in received_types
    assert "text" in received_types
    assert "user_text" in received_types


def _send_session_data_and_verify_response(ws):
    """Helper to send standard test payload (image, audio, stop) and verify responses."""
    ws.send_text(json.dumps({"type": "image", "data": base64.b64encode(b"fakeimage").decode("utf-8")}))
    ws.send_text(json.dumps({"type": "audio", "data": base64.b64encode(b"fakeaudio").decode("utf-8")}))
    ws.send_text(json.dumps({"type": "stop"}))
    _verify_standard_responses(ws)


@patch("backend.app.main.ADKGemini")
def test_websocket_chat_vertexai_agent(mock_gemini_class):
    setup_mock_gemini_connection(mock_gemini_class)

    with client.websocket_connect(
        "/api/chat?vertexai=true&project=my-project&location=us-central1&agent_id=my-agent"
    ) as ws:
        _send_session_data_and_verify_response(ws)

    mock_gemini_class.assert_called_once_with(
        model="projects/my-project/locations/us-central1/agents/my-agent",
        use_vertexai_flag=True,
        project="my-project",
        location="us-central1",
    )


@patch("backend.app.main.ADKGemini")
def test_websocket_chat_vertexai_direct_model(mock_gemini_class):
    setup_mock_gemini_connection(mock_gemini_class)

    with client.websocket_connect("/api/chat?vertexai=true&model=publishers/google/models/gemini-2.0-flash-exp") as ws:
        _send_session_data_and_verify_response(ws)


@patch("backend.app.main.ADKGemini")
def test_websocket_chat_vertexai_direct_model_with_params(mock_gemini_class):
    setup_mock_gemini_connection(mock_gemini_class)

    with client.websocket_connect(
        "/api/chat?vertexai=true&model=publishers/google/models/gemini-2.0-flash-exp&project=my-project&location=europe-west4"
    ) as ws:
        _send_session_data_and_verify_response(ws)

    mock_gemini_class.assert_called_once_with(
        model="publishers/google/models/gemini-2.0-flash-exp",
        use_vertexai_flag=True,
        project="my-project",
        location="europe-west4",
    )


@patch("backend.app.main.ADKGemini")
def test_websocket_chat_standard_gemini(mock_gemini_class):
    mock_connection = setup_mock_gemini_connection(mock_gemini_class)

    with client.websocket_connect("/api/chat?vertexai=false&model=gemini-2.5-flash-native-audio-preview-12-2025") as ws:
        _send_session_data_and_verify_response(ws)

        # Verify connection.send_realtime_input was called
        mock_connection._gemini_session.send_realtime_input.assert_called()


def test_websocket_chat_invalid_parameters():
    # 1. Test invalid voice format
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect("/api/chat?voice=invalid;voice") as ws:
        ws.receive_text()
    assert excinfo.value.code == 1011

    # 2. Test invalid gcp project format
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            "/api/chat?vertexai=true&project=invalid/project&location=us-central1&agent_id=my-agent"
        ) as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011

    # 3. Test invalid gemini model format
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/chat?vertexai=false&model=invalid/model") as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011

    # 4. Test invalid resumption token format
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/chat?resumption_token=invalid;token") as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011

    # 5. Test too long instruction
    long_instruction = "a" * 4097
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(f"/api/chat?instruction={long_instruction}") as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011

    # 6. Test invalid direct Vertex AI model format
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/chat?vertexai=true&model=publishers/invalid;model") as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011

    # 7. Test invalid vertexai parameter format
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/chat?vertexai=invalid") as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011


def test_create_live_connect_config():
    config = create_live_connect_config(
        use_vertexai=False,
        gcp_agent_id=None,
        voice_name="Puck",
        resumption_token=None,
    )
    assert config.realtime_input_config is not None
    assert config.realtime_input_config.activity_handling == types.ActivityHandling.NO_INTERRUPTION


@patch("backend.app.main.ADKGemini")
def test_websocket_chat_client_message_validation(mock_gemini_class):
    mock_connection = setup_mock_gemini_connection(mock_gemini_class)

    with client.websocket_connect("/api/chat?vertexai=false&model=gemini-2.5-flash-native-audio-preview-12-2025") as ws:
        # 1. Send normal text message
        ws.send_text(json.dumps({"type": "text", "data": "Hello assistant"}))

        # 2. Send too long text message (should be ignored and not raise error or block)
        ws.send_text(json.dumps({"type": "text", "data": "a" * 16385}))

        # 3. Send invalid type message
        ws.send_text(json.dumps({"type": "text", "data": 1234}))

        # 4. Send text message with null byte (should be safely dropped)
        ws.send_text(json.dumps({"type": "text", "data": "Bad\x00prompt"}))

        # 5. Send whitespace-only text message (should be safely dropped)
        ws.send_text(json.dumps({"type": "text", "data": "   "}))

        # 6. Send oversized type name (should be dropped)
        ws.send_text(json.dumps({"type": "x" * 50, "data": "test"}))

        # 7. Send stop message
        ws.send_text(json.dumps({"type": "stop"}))

        # Receive mock responses from WebSocket to ensure connection wasn't closed by errors
        _verify_standard_responses(ws)

        # Verify connection.send_realtime_input was called
        mock_connection._gemini_session.send_realtime_input.assert_called()


def test_is_managed_agent_config():

    assert is_managed_agent_config(use_vertexai=True, gcp_agent_id="my-agent") is True
    assert is_managed_agent_config(use_vertexai=True, gcp_agent_id=None) is False
    assert is_managed_agent_config(use_vertexai=False, gcp_agent_id="my-agent") is False
    assert is_managed_agent_config(use_vertexai=False, gcp_agent_id=None) is False


def test_extract_gemini_events_all_branches():
    async def _test():
        # 1. Interrupted
        mock_interrupted = MagicMock()
        mock_interrupted.interrupted = True
        events = await extract_gemini_events(mock_interrupted)
        assert events == [{"type": "interrupt"}]

        # 2. Resumption update & go_away & transcription & content
        mock_resp = MagicMock()
        mock_resp.interrupted = False
        mock_resp.live_session_resumption_update = MagicMock(new_handle="token_123")
        mock_resp.go_away = MagicMock(time_left="5s")
        mock_resp.input_transcription = MagicMock(text="こんにちは")

        part1 = MagicMock()
        part1.text = "返信テキスト"
        part1.inline_data = None

        part2 = MagicMock()
        part2.text = None
        part2.inline_data = MagicMock(data=b"raw_audio")

        mock_resp.content = MagicMock(parts=[part1, part2])

        events = await extract_gemini_events(mock_resp)
        assert len(events) == 5
        assert events[0] == {"type": "resumption_token", "data": {"handle": "token_123"}}
        assert events[1] == {"type": "go_away", "data": {"time_left": "5s"}}
        assert events[2] == {"type": "user_text", "data": "こんにちは"}
        assert events[3] == {"type": "text", "data": "返信テキスト"}
        assert events[4]["type"] == "audio"
        assert base64.b64decode(events[4]["data"]) == b"raw_audio"

    asyncio.run(_test())


def test_unit_send_realtime_input():
    async def _test():
        # 1. Test uninitialized session
        conn_no_session = MagicMock(spec=[])
        res = await _send_realtime_input(conn_no_session, text="hello")
        assert res is False

        # 2. Test audio_stream_end exception is gracefully caught
        mock_session = MagicMock()
        mock_session.send_realtime_input.side_effect = RuntimeError("session closed")
        conn_with_err = MagicMock(_gemini_session=mock_session)
        res = await _send_realtime_input(conn_with_err, audio_stream_end=True)
        assert res is False

        # 3. Test generic exception is re-raised
        with pytest.raises(RuntimeError):
            await _send_realtime_input(conn_with_err, text="crash")

    asyncio.run(_test())


def test_unit_decode_and_send_blob():
    async def _test():
        conn = MagicMock()
        conn._gemini_session = MagicMock()

        # 1. Payload exceeding max_base64_payload_len is rejected
        large_payload = "A" * (settings.max_base64_payload_len + 10)
        await decode_and_send_blob(conn, large_payload, "audio")
        conn._gemini_session.send_realtime_input.assert_not_called()

        # 2. Corrupt base64 string is rejected
        await decode_and_send_blob(conn, "not_base_64!@#$", "audio")
        conn._gemini_session.send_realtime_input.assert_not_called()

        # 3. Unsupported msg_type is rejected
        valid_b64 = base64.b64encode(b"valid").decode("utf-8")
        await decode_and_send_blob(conn, valid_b64, "unsupported_type")
        conn._gemini_session.send_realtime_input.assert_not_called()

        # 4. Empty base64 payload is rejected
        await decode_and_send_blob(conn, "", "audio")
        conn._gemini_session.send_realtime_input.assert_not_called()

    asyncio.run(_test())


def test_unit_validate_parameter():
    async def _test():
        ws = MagicMock()
        ws.close = MagicMock()

        # 1. Missing required parameter
        pattern = re.compile(r"^[a-z]+$")
        res = await validate_parameter(ws, None, pattern, "param", required=True)
        assert res is False

        # 2. Missing optional parameter
        res = await validate_parameter(ws, None, pattern, "param", required=False)
        assert res is True

        # 3. Placeholder rejected
        res = await validate_parameter(
            ws, "YOUR_PLACEHOLDER", pattern, "param", placeholder=("YOUR_PLACEHOLDER", "test")
        )
        assert res is False

        # 4. Pattern mismatch rejected
        res = await validate_parameter(ws, "12345", pattern, "param")
        assert res is False

        # 5. Valid parameter accepted
        res = await validate_parameter(ws, "validparam", pattern, "param")
        assert res is True

    asyncio.run(_test())


def test_mask_token():
    assert mask_token("12345") == "***"
    assert mask_token("12345678") == "***"
    assert mask_token("123456789") == "1234...6789"
    assert mask_token("token_abcdef123456_xyz") == "toke..._xyz"
    assert mask_token(None) == ""
    assert mask_token("") == ""
    assert mask_token(123456789) == "1234...6789"


def test_websocket_chat_vertexai_direct_model_invalid_project_location():
    # 1. Invalid project in direct Vertex AI model route
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            "/api/chat?vertexai=true&model=publishers/google/models/gemini-2.0-flash-exp&project=invalid/project"
        ) as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011

    # 2. Invalid location in direct Vertex AI model route
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect(
            "/api/chat?vertexai=true&model=publishers/google/models/gemini-2.0-flash-exp&location=invalid;loc"
        ) as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011


def test_config_get_int_env_bounds(monkeypatch):
    monkeypatch.setenv("TEST_INT_BOUNDS", "50")
    assert _get_int_env("TEST_INT_BOUNDS", default=10, min_val=10, max_val=100) == 50

    # Below min_val -> falls back to default
    monkeypatch.setenv("TEST_INT_BOUNDS", "5")
    assert _get_int_env("TEST_INT_BOUNDS", default=10, min_val=10, max_val=100) == 10

    # Above max_val -> falls back to default
    monkeypatch.setenv("TEST_INT_BOUNDS", "150")
    assert _get_int_env("TEST_INT_BOUNDS", default=10, min_val=10, max_val=100) == 10

    # Non-integer -> falls back to default
    monkeypatch.setenv("TEST_INT_BOUNDS", "not_an_int")
    assert _get_int_env("TEST_INT_BOUNDS", default=10, min_val=10, max_val=100) == 10


def test_safe_close_websocket_sanitization():
    async def _test():
        mock_ws = MagicMock()

        async def mock_close(*args, **kwargs):
            pass

        mock_ws.close = MagicMock(side_effect=mock_close)

        await safe_close_websocket(mock_ws, code=1008, reason="Error line 1\r\nError line 2\x00")
        mock_ws.close.assert_called_once()
        _, kwargs = mock_ws.close.call_args
        reason = kwargs.get("reason", "")
        assert "\r" not in reason
        assert "\n" not in reason
        assert "\x00" not in reason
        assert "Error line 1  Error line 2" in reason

    asyncio.run(_test())


def test_sanitize_close_reason():
    sanitized = sanitize_close_reason("Error line 1\r\nError line 2\x00" + "A" * 200)
    assert "\r" not in sanitized
    assert "\n" not in sanitized
    assert "\x00" not in sanitized
    assert len(sanitized.encode("utf-8")) <= 123


def test_websocket_chat_vertexai_direct_model_unsupported_prefix():
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/chat?vertexai=true&model=custom_model_without_prefix") as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011


def test_websocket_chat_instruction_null_byte():
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/chat?instruction=hello%00world") as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011


def test_websocket_chat_header_authentication():
    settings.API_ACCESS_KEY = "super_secret_key"
    try:
        # 1. Test connection with valid X-API-Key header
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/chat", headers={"x-api-key": "super_secret_key"}) as ws,
        ):
            ws.receive_text()
        assert excinfo.value.code == 1011

        # 2. Test connection with valid Authorization Bearer header
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/chat", headers={"authorization": "Bearer super_secret_key"}) as ws,
        ):
            ws.receive_text()
        assert excinfo.value.code == 1011

        # 3. Test connection with invalid X-API-Key header
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/chat", headers={"x-api-key": "wrong_key"}) as ws,
        ):
            ws.receive_text()
        assert excinfo.value.code == 4003

        # 4. Test connection with invalid Authorization header
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/chat", headers={"authorization": "Bearer wrong_key"}) as ws,
        ):
            ws.receive_text()
        assert excinfo.value.code == 4003
    finally:
        settings.API_ACCESS_KEY = None


def test_is_disconnect_error():
    assert _is_disconnect_error(RuntimeError("WebSocket disconnect")) is True
    assert (
        _is_disconnect_error(RuntimeError("Cannot call 'receive' once a disconnect message has been received")) is True
    )
    assert _is_disconnect_error(ConnectionResetError("Connection reset by peer")) is True
    assert _is_disconnect_error(RuntimeError("connection closed")) is True
    assert _is_disconnect_error(RuntimeError("not connected")) is True
    assert _is_disconnect_error(ValueError("Invalid argument")) is False
    assert _is_disconnect_error(TimeoutError("Request timed out")) is False


def test_sanitize_for_log():
    assert sanitize_for_log("") == ""
    assert sanitize_for_log("Hello World") == "Hello World"
    # Control characters replaced
    assert sanitize_for_log("Hello\r\nWorld\x00\x1b[31mRed\x1b[0m") == "Hello  World  [31mRed [0m"
    # Length truncation
    long_text = "A" * 300
    res = sanitize_for_log(long_text, max_length=10)
    assert res == "AAAAAAAAAA..."


def test_sanitize_close_reason_extended():
    reason = "Error with ESC \x1b and BEL \x07 characters"
    sanitized = sanitize_close_reason(reason)
    assert "\x1b" not in sanitized
    assert "\x07" not in sanitized
    assert len(sanitized.encode("utf-8")) <= 123


def test_redact_sensitive_keys():
    settings.API_ACCESS_KEY = "my_access_secret_123"
    settings.GOOGLE_API_KEY = "my_google_api_key_456"
    try:
        assert redact_sensitive_keys("") == ""
        assert redact_sensitive_keys(None) == ""
        # Redact single key
        text1 = "Error connecting with my_access_secret_123 to backend"
        assert redact_sensitive_keys(text1) == "Error connecting with [REDACTED] to backend"
        # Redact both keys
        text2 = "Keys: my_access_secret_123 and my_google_api_key_456"
        assert redact_sensitive_keys(text2) == "Keys: [REDACTED] and [REDACTED]"
        # No key present
        text3 = "Normal message without credentials"
        assert redact_sensitive_keys(text3) == text3
    finally:
        settings.API_ACCESS_KEY = None
        settings.GOOGLE_API_KEY = None


def test_websocket_chat_header_authentication_bearer_case_insensitive():
    settings.API_ACCESS_KEY = "super_secret_key"
    try:
        # Test lowercase "bearer" token scheme
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/chat", headers={"authorization": "bearer super_secret_key"}) as ws,
        ):
            ws.receive_text()
        # Code 1011 indicates authentication succeeded and proceeded to model config check
        assert excinfo.value.code == 1011
    finally:
        settings.API_ACCESS_KEY = None


def test_validate_parameter_log_sanitization():
    async def _test():
        ws = MagicMock()
        ws.close = MagicMock()

        pattern = re.compile(r"^[a-z]+$")
        # Value contains CRLF injection attempt
        malicious_val = "bad\r\ninjected_log\x00data"
        with patch("backend.app.main.logger") as mock_logger:
            res = await validate_parameter(ws, malicious_val, pattern, "test_param")
            assert res is False
            # Verify the logged message was sanitized and contains no CRLF or null bytes
            mock_logger.error.assert_called_once()
            logged_error = mock_logger.error.call_args[0][0]
            assert "\r" not in logged_error
            assert "\n" not in logged_error
            assert "\x00" not in logged_error

    asyncio.run(_test())


def test_safe_close_websocket_already_closed_states():
    async def _test():
        mock_ws = MagicMock()

        # 1. client_state is DISCONNECTED -> should return immediately without calling close
        mock_ws.client_state = WebSocketState.DISCONNECTED
        mock_ws.application_state = WebSocketState.CONNECTED
        await safe_close_websocket(mock_ws, code=1000, reason="Normal closure")
        mock_ws.close.assert_not_called()

        # 2. application_state is DISCONNECTED -> should return immediately without calling close
        mock_ws.client_state = WebSocketState.CONNECTED
        mock_ws.application_state = WebSocketState.DISCONNECTED
        await safe_close_websocket(mock_ws, code=1000, reason="Normal closure")
        mock_ws.close.assert_not_called()

    asyncio.run(_test())


def test_safe_close_websocket_redaction():
    async def _test():
        settings.API_ACCESS_KEY = "secret_access_key"
        try:
            mock_ws = MagicMock()
            mock_ws.client_state = WebSocketState.CONNECTED
            mock_ws.application_state = WebSocketState.CONNECTED

            async def mock_close(*args, **kwargs):
                pass

            mock_ws.close = MagicMock(side_effect=mock_close)

            reason = "Failed with secret_access_key authentication"
            await safe_close_websocket(mock_ws, code=1011, reason=reason)
            mock_ws.close.assert_called_once()
            _, kwargs = mock_ws.close.call_args
            assert "secret_access_key" not in kwargs["reason"]
            assert "[REDACTED]" in kwargs["reason"]
        finally:
            settings.API_ACCESS_KEY = None

    asyncio.run(_test())


def test_sanitize_for_log_non_string():
    assert sanitize_for_log(12345) == "12345"
    assert sanitize_for_log(RuntimeError("Sample error\r\nwith newline")) == "Sample error  with newline"


def test_sanitize_close_reason_none_and_non_string():
    assert sanitize_close_reason(None) == ""
    assert sanitize_close_reason("") == ""
    assert sanitize_close_reason(12345) == "12345"
    assert sanitize_close_reason(ValueError("Close reason")) == "Close reason"


def test_redact_sensitive_keys_non_string():
    settings.API_ACCESS_KEY = "my_access_secret_123"
    try:
        assert redact_sensitive_keys(12345) == "12345"
        err = RuntimeError("Failed with my_access_secret_123 in call")
        assert redact_sensitive_keys(err) == "Failed with [REDACTED] in call"
    finally:
        settings.API_ACCESS_KEY = None


def test_extract_gemini_events_output_transcription():
    async def _test():
        # 1. Normal output transcription
        mock_resp = MagicMock()
        mock_resp.interrupted = False
        mock_resp.live_session_resumption_update = None
        mock_resp.go_away = None
        mock_resp.input_transcription = None
        mock_resp.output_transcription = MagicMock(text="はい、目の前の風景を説明します。")
        mock_resp.content = None

        events = await extract_gemini_events(mock_resp)
        assert len(events) == 1
        assert events[0] == {"type": "text", "data": "はい、目の前の風景を説明します。"}

        # 2. Empty string output transcription
        mock_resp_empty = MagicMock()
        mock_resp_empty.interrupted = False
        mock_resp_empty.live_session_resumption_update = None
        mock_resp_empty.go_away = None
        mock_resp_empty.input_transcription = None
        mock_resp_empty.output_transcription = MagicMock(text="")
        mock_resp_empty.content = None

        events_empty = await extract_gemini_events(mock_resp_empty)
        assert len(events_empty) == 0

    asyncio.run(_test())


def test_validate_parameter_placeholder_variations():
    async def _test():
        ws = MagicMock()
        ws.close = MagicMock()
        pattern = re.compile(r"^[a-z0-9-]+$")

        # Tuple placeholder
        assert (
            await validate_parameter(
                ws, "valid-val", pattern, "test_param", placeholder=("placeholder1", "placeholder2")
            )
            is True
        )
        assert (
            await validate_parameter(
                ws, "placeholder1", pattern, "test_param", placeholder=("placeholder1", "placeholder2")
            )
            is False
        )

        # Single string placeholder
        assert (
            await validate_parameter(ws, "your-placeholder", pattern, "test_param", placeholder="your-placeholder")
            is False
        )
        assert (
            await validate_parameter(ws, "actual-value", pattern, "test_param", placeholder="your-placeholder") is True
        )

        # None placeholder
        assert await validate_parameter(ws, "valid-value", pattern, "test_param", placeholder=None) is True

    asyncio.run(_test())


def test_log_stream_exception():
    with patch("backend.app.main.logger") as mock_logger:
        # 1. Disconnect error
        _log_stream_exception("test_loop", RuntimeError("WebSocket connection closed"))
        mock_logger.info.assert_called_once()
        assert "Connection disconnected in test_loop" in mock_logger.info.call_args[0][0]

        # 2. Unexpected error
        _log_stream_exception("test_loop", ValueError("Unexpected payload format"))
        mock_logger.warning.assert_called_once()
        assert "Error in test_loop" in mock_logger.warning.call_args[0][0]


def test_websocket_chat_header_authentication_x_api_key_whitespace():
    settings.API_ACCESS_KEY = "super_secret_key"
    try:
        # Test X-API-Key with leading/trailing whitespace
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/chat", headers={"x-api-key": "  super_secret_key  "}) as ws,
        ):
            ws.receive_text()
        # Code 1011 indicates authentication succeeded and proceeded to model config check
        assert excinfo.value.code == 1011
    finally:
        settings.API_ACCESS_KEY = None


def test_extract_auth_token():
    mock_ws = MagicMock()
    mock_ws.headers = {}

    # 1. Query parameter key provided with whitespace
    assert _extract_auth_token(mock_ws, "  my_query_key  ") == "my_query_key"

    # 2. X-API-Key header provided
    mock_ws.headers = {"x-api-key": "  my_header_key  "}
    assert _extract_auth_token(mock_ws, None) == "my_header_key"

    # 3. Authorization Bearer header provided
    mock_ws.headers = {"authorization": "Bearer   bearer_token_123  "}
    assert _extract_auth_token(mock_ws, None) == "bearer_token_123"

    # 4. Authorization bearer lowercase
    mock_ws.headers = {"authorization": "bearer my_lower_token"}
    assert _extract_auth_token(mock_ws, None) == "my_lower_token"

    # 5. Missing / empty credentials
    mock_ws.headers = {}
    assert _extract_auth_token(mock_ws, None) is None
    assert _extract_auth_token(mock_ws, "") is None
    assert _extract_auth_token(mock_ws, "   ") is None


def test_extract_transcription_text():
    class MockTranscription:
        def __init__(self, text):
            self.text = text

    # Valid string text
    assert _extract_transcription_text(MockTranscription("Hello world")) == "Hello world"

    # None or missing text
    assert _extract_transcription_text(MockTranscription(None)) is None
    assert _extract_transcription_text(MockTranscription("")) is None
    assert _extract_transcription_text(None) is None

    # Non-string object
    assert _extract_transcription_text(MockTranscription(12345)) is None


def test_is_disconnect_error_extended():
    assert _is_disconnect_error(BrokenPipeError("Broken pipe")) is True
    assert _is_disconnect_error(ConnectionResetError("Connection reset by peer")) is True
    assert _is_disconnect_error(WebSocketDisconnect(1000, "Normal closure")) is True


def test_mask_token_crlf_and_non_printable():
    assert mask_token(None) == ""
    assert mask_token("") == ""
    assert mask_token("short") == "***"
    # Token with CRLF or non-printable chars stripped
    token_with_crlf = "abcd\r\n12345678\x00wxyz"
    masked = mask_token(token_with_crlf)
    assert "\r" not in masked
    assert "\n" not in masked
    assert "\x00" not in masked
    assert masked.startswith("abcd")
    assert masked.endswith("wxyz")


def test_get_str_env(monkeypatch):
    monkeypatch.setenv("TEST_ENV_STR", "   valid_string   ")
    assert _get_str_env("TEST_ENV_STR") == "valid_string"

    monkeypatch.setenv("TEST_ENV_EMPTY", "   ")
    assert _get_str_env("TEST_ENV_EMPTY", default="fallback") == "fallback"

    monkeypatch.delenv("TEST_ENV_UNSET", raising=False)
    assert _get_str_env("TEST_ENV_UNSET", default="fallback") == "fallback"


def test_validate_parameter_collections():
    async def _test():
        ws = MagicMock()
        ws.close = MagicMock()
        pattern = re.compile(r"^[a-z]+$")

        # Placeholder as list
        res_list = await validate_parameter(
            ws, "placeholder_val", pattern, "test_param", placeholder=["placeholder_val"]
        )
        assert res_list is False

        # Placeholder as set
        res_set = await validate_parameter(
            ws, "placeholder_val", pattern, "test_param", placeholder={"placeholder_val"}
        )
        assert res_set is False

    asyncio.run(_test())


def test_decode_and_send_blob_non_string():
    async def _test():
        mock_conn = MagicMock()
        with patch("backend.app.main.logger") as mock_logger:
            # None or non-string base64_data rejected safely
            await decode_and_send_blob(mock_conn, None, "audio")
            mock_logger.warning.assert_called_once()
            assert "Rejected empty or non-string" in mock_logger.warning.call_args[0][0]

            # Unsupported msg_type sanitized in log
            mock_logger.reset_mock()
            await decode_and_send_blob(mock_conn, "valid_b64", "malicious\r\ntype")
            mock_logger.warning.assert_called_once()
            assert "\r" not in mock_logger.warning.call_args[0][0]

    asyncio.run(_test())


def test_safe_b64encode_and_decode():
    async def _test():
        # 1. String pass-through for encode
        assert await _safe_b64encode("already_string") == "already_string"

        # 2. Small bytes encoding and decoding
        raw = b"Hello, Base64 world!"
        encoded = await _safe_b64encode(raw)
        assert isinstance(encoded, str)
        decoded = await _safe_b64decode(encoded)
        assert decoded == raw

        # 3. Large payload offloaded to thread (over B64_OFFLOAD_THRESHOLD_BYTES)
        large_raw = b"X" * (settings.B64_OFFLOAD_THRESHOLD_BYTES + 2048)
        large_encoded = await _safe_b64encode(large_raw)
        assert isinstance(large_encoded, str)
        large_decoded = await _safe_b64decode(large_encoded)
        assert large_decoded == large_raw

        # 4. Bytearray and memoryview support
        barr = bytearray(b"test bytearray")
        assert await _safe_b64encode(barr) == base64.b64encode(b"test bytearray").decode("ascii")
        mview = memoryview(b"test memoryview")
        assert await _safe_b64encode(mview) == base64.b64encode(b"test memoryview").decode("ascii")

        # 5. Whitespace trimming in _safe_b64decode
        padded_encoded = f"  \n  {encoded}  \r\n  "
        assert await _safe_b64decode(padded_encoded) == raw

        # 6. Invalid base64 raises binascii.Error / ValueError
        with pytest.raises(binascii.Error):
            await _safe_b64decode("invalid_b64!!!@@@")

    asyncio.run(_test())


def test_normalize_param():
    assert _normalize_param(None) is None
    assert _normalize_param(123) is None
    assert _normalize_param("") is None
    assert _normalize_param("   ") is None
    assert _normalize_param("\t\r\n") is None
    assert _normalize_param("  valid_param  ") == "valid_param"
    assert _normalize_param("param") == "param"


def test_validated_compression_target():
    original_trigger = settings.GEMINI_COMPRESSION_TRIGGER
    original_target = settings.GEMINI_COMPRESSION_TARGET
    try:
        settings.GEMINI_COMPRESSION_TRIGGER = 25000
        settings.GEMINI_COMPRESSION_TARGET = 8000
        assert settings.validated_compression_target == 8000

        # When misconfigured so target >= trigger
        settings.GEMINI_COMPRESSION_TRIGGER = 10000
        settings.GEMINI_COMPRESSION_TARGET = 15000
        assert settings.validated_compression_target == 5000

        # When trigger // 2 is very low, should enforce minimum 500
        settings.GEMINI_COMPRESSION_TRIGGER = 800
        settings.GEMINI_COMPRESSION_TARGET = 1000
        assert settings.validated_compression_target == 500
    finally:
        settings.GEMINI_COMPRESSION_TRIGGER = original_trigger
        settings.GEMINI_COMPRESSION_TARGET = original_target


def test_get_bool_env(monkeypatch):
    monkeypatch.setenv("TEST_BOOL_TRUE", "true")
    monkeypatch.setenv("TEST_BOOL_FALSE", "false")
    monkeypatch.setenv("TEST_BOOL_EMPTY", "   ")
    assert _get_bool_env("TEST_BOOL_TRUE", False) is True
    assert _get_bool_env("TEST_BOOL_FALSE", True) is False
    assert _get_bool_env("TEST_BOOL_EMPTY", True) is True
    assert _get_bool_env("NON_EXISTENT_KEY", False) is False


def test_redact_sensitive_keys_dynamic_tokens():
    # Test query param ?key=... and &token=... redaction
    url1 = "https://generativelanguage.googleapis.com/v1beta/live?key=AIzaSySecretApiKey123"
    assert redact_sensitive_keys(url1) == "https://generativelanguage.googleapis.com/v1beta/live?key=[REDACTED]"

    url2 = "https://example.com/api?user=alice&token=Secr3t_Token.Val-999&action=test"
    assert redact_sensitive_keys(url2) == "https://example.com/api?user=alice&token=[REDACTED]&action=test"

    # Test Bearer token redaction
    auth_header = "Authorization: Bearer my-super-secret-jwt-token-xyz"
    assert redact_sensitive_keys(auth_header) == "Authorization: Bearer [REDACTED]"


def test_log_stream_exception_sanitization_and_redaction():
    with patch("backend.app.main.logger") as mock_logger:
        # Disconnect error logged at info with sanitized/redacted error
        disc_err = ConnectionResetError("Connection reset by peer: key=SecretKey123\r\ninjected")
        _log_stream_exception("test_loop", disc_err)
        mock_logger.info.assert_called_once()
        assert "key=[REDACTED]" in mock_logger.info.call_args[0][0]
        assert "\r" not in mock_logger.info.call_args[0][0]

        # Non-disconnect error logged at warning
        mock_logger.reset_mock()
        warn_err = RuntimeError("Unexpected internal error with token=SensitiveData")
        _log_stream_exception("test_loop", warn_err)
        mock_logger.warning.assert_called_once()
        assert "token=[REDACTED]" in mock_logger.warning.call_args[0][0]


def test_safe_close_websocket_invalid_code():
    async def _test():
        mock_ws = MagicMock()
        mock_ws.client_state = WebSocketState.CONNECTED
        mock_ws.application_state = WebSocketState.CONNECTED
        mock_ws.close = AsyncMock()

        # Invalid code < 1000 or > 4999 defaults to 1011
        await safe_close_websocket(mock_ws, code=500, reason="Invalid code test")
        mock_ws.close.assert_called_with(code=1011, reason="Invalid code test")

        mock_ws.close.reset_mock()
        await safe_close_websocket(mock_ws, code=9999, reason="Too high code test")
        mock_ws.close.assert_called_with(code=1011, reason="Too high code test")

        mock_ws.close.reset_mock()
        await safe_close_websocket(mock_ws, code="invalid", reason="Non-int code test")
        mock_ws.close.assert_called_with(code=1011, reason="Non-int code test")

    asyncio.run(_test())


def test_extract_gemini_events_defensive():
    async def _test():
        # None response returns empty list
        assert await extract_gemini_events(None) == []

        # Response with empty/invalid content parts
        resp = MagicMock()
        resp.interrupted = False
        resp.live_session_resumption_update = None
        resp.go_away = None
        resp.input_transcription = None
        resp.output_transcription = None

        # parts is not a list/tuple
        resp.content.parts = "invalid_parts_string"
        assert await extract_gemini_events(resp) == []

        # inline_data has empty data
        part = MagicMock()
        part.text = None
        part.inline_data.data = b""
        resp.content.parts = [part]
        assert await extract_gemini_events(resp) == []

    asyncio.run(_test())


def test_adk_gemini_connect_content_system_instruction():
    mock_client = MagicMock()
    mock_live = MagicMock()
    mock_connect = MagicMock()
    mock_connect.__aenter__.return_value = MagicMock()
    mock_connect.__aexit__.return_value = None
    mock_live.connect.return_value = mock_connect
    mock_client.aio.live = mock_live

    mock_request = MagicMock()
    mock_request.model = "gemini-2.5-flash-native-audio-latest"
    content_instruction = types.Content(
        role="system",
        parts=[types.Part.from_text(text="Custom Content Instruction")],
    )
    mock_request.config.system_instruction = content_instruction
    mock_request.config.tools = None

    async def _test():
        with patch.object(ADKGemini, "_init_client", return_value=mock_client):
            adk = ADKGemini(
                model="gemini-2.5-flash-native-audio-latest",
                use_vertexai=False,
            )
            async with adk.connect(mock_request) as conn:
                assert conn is not None
                called_config = mock_live.connect.call_args[1]["config"]
                assert called_config.system_instruction == content_instruction

    asyncio.run(_test())


def test_http_security_headers():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("content-security-policy") == "default-src 'none'; frame-ancestors 'none'"
    assert response.headers.get("x-xss-protection") == "0"


def test_redact_sensitive_keys_expanded():
    text = (
        "url?api_key=secret123&apikey=key456&access_token=token789&secret=my_secret&password=pass123 "
        "and Bearer my_jwt_token"
    )
    redacted = redact_sensitive_keys(text)
    assert "secret123" not in redacted
    assert "key456" not in redacted
    assert "token789" not in redacted
    assert "my_secret" not in redacted
    assert "pass123" not in redacted
    assert "my_jwt_token" not in redacted
    assert "[REDACTED]" in redacted


def test_websocket_chat_instruction_control_chars():
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/chat?instruction=hello%1b[31mred%1b[0m") as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 1011


def test_adk_gemini_use_vertexai_alias():
    adk_v = ADKGemini(model="test-model", use_vertexai=True)
    assert adk_v.use_vertexai_flag is True
    adk_f = ADKGemini(model="test-model", use_vertexai=False)
    assert adk_f.use_vertexai_flag is False


def test_extract_auth_token_api_key_header():
    ws_mock = MagicMock()
    ws_mock.headers = {"api-key": "test_api_key_value"}
    assert _extract_auth_token(ws_mock, None) == "test_api_key_value"


def test_extract_transcription_text_dict():
    assert _extract_transcription_text({"text": "  Dict transcription  "}) == "Dict transcription"
    assert _extract_transcription_text({"text": "   "}) is None
    assert _extract_transcription_text({}) is None


def test_safe_b64decode_bytes_input():
    raw = b"Hello Antigravity!"
    b64_bytes = base64.b64encode(raw)
    decoded = asyncio.run(_safe_b64decode(b64_bytes))
    assert decoded == raw


def test_extract_gemini_events_dict_parts():
    mock_resp = type(
        "MockResp",
        (),
        {
            "interrupted": False,
            "live_session_resumption_update": None,
            "go_away": None,
            "input_transcription": None,
            "output_transcription": None,
            "content": type(
                "Content",
                (),
                {
                    "parts": [
                        {"text": "Dict Text Event"},
                        {"inline_data": {"data": b"mock_audio_data"}},
                    ]
                },
            )(),
        },
    )()

    events = asyncio.run(extract_gemini_events(mock_resp))
    assert len(events) == 2
    assert events[0] == {"type": "text", "data": "Dict Text Event"}
    assert events[1]["type"] == "audio"


def test_get_field_helper():
    # Test dictionary access
    d = {"name": "Alice", "val": 100}
    assert _get_field(d, "name") == "Alice"
    assert _get_field(d, "missing", default="none") == "none"

    # Test object attribute access
    class DummyObj:
        def __init__(self):
            self.title = "test_title"

    obj = DummyObj()
    assert _get_field(obj, "title") == "test_title"
    assert _get_field(obj, "unknown", default=42) == 42

    # Test None object
    assert _get_field(None, "anything", default="fallback") == "fallback"


def test_extract_auth_token_additional_schemes():
    ws_mock = MagicMock()
    # Test ApiKey prefix
    ws_mock.headers = {"authorization": "ApiKey key_abc_123"}
    assert _extract_auth_token(ws_mock, None) == "key_abc_123"

    # Test Token prefix
    ws_mock.headers = {"authorization": "Token token_xyz_456"}
    assert _extract_auth_token(ws_mock, None) == "token_xyz_456"


def test_redact_sensitive_keys_extra_schemes():
    text = "ApiKey secretKeyVal and Token secretTokenVal with ?auth=authSecret123&authorization=authSecret456"
    redacted = redact_sensitive_keys(text)
    assert "secretKeyVal" not in redacted
    assert "secretTokenVal" not in redacted
    assert "authSecret123" not in redacted
    assert "authSecret456" not in redacted
    assert "[REDACTED]" in redacted


def test_websocket_chat_origin_validation():
    orig_origins = settings.ALLOWED_ORIGINS
    try:
        settings.ALLOWED_ORIGINS = "https://trusted.example.com"

        # Disallowed origin should be rejected with 4003
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/chat", headers={"origin": "https://malicious.example.com"}) as ws,
        ):
            ws.receive_text()
        assert excinfo.value.code == 4003

        # Allowed origin should proceed past origin check
        with patch("backend.app.main.ADKGemini") as mock_gemini_class:
            setup_mock_gemini_connection(mock_gemini_class)
            with client.websocket_connect(
                "/api/chat?vertexai=false&model=gemini-2.5-flash-native-audio-preview-12-2025",
                headers={"origin": "https://trusted.example.com"},
            ) as ws:
                _verify_standard_responses(ws)
    finally:
        settings.ALLOWED_ORIGINS = orig_origins


def test_allowed_origins_set_property():
    orig = settings.ALLOWED_ORIGINS
    try:
        settings.ALLOWED_ORIGINS = "*"
        assert settings.allowed_origins_set == {"*"}

        settings.ALLOWED_ORIGINS = "https://foo.com, https://bar.com "
        assert settings.allowed_origins_set == {"https://foo.com", "https://bar.com"}

        settings.ALLOWED_ORIGINS = ""
        assert settings.allowed_origins_set == {"*"}

        settings.ALLOWED_ORIGINS = "   "
        assert settings.allowed_origins_set == {"*"}

        settings.ALLOWED_ORIGINS = "https://foo.com, *"
        assert settings.allowed_origins_set == {"*"}
    finally:
        settings.ALLOWED_ORIGINS = orig


def test_extract_auth_token_x_goog_api_key():
    ws_mock = MagicMock()
    ws_mock.headers = {"x-goog-api-key": "google_api_key_789"}
    assert _extract_auth_token(ws_mock, None) == "google_api_key_789"


def test_http_security_headers_comprehensive():
    response = client.get("/health")
    assert response.status_code == 200
    headers = response.headers
    assert "accelerometer=()" in headers.get("permissions-policy", "")
    assert headers.get("cross-origin-opener-policy") == "same-origin"
    assert headers.get("cross-origin-resource-policy") == "same-origin"


def test_instruction_normalized_via_normalize_param():
    with patch("backend.app.main.ADKGemini") as mock_gemini_class:
        setup_mock_gemini_connection(mock_gemini_class)
        with client.websocket_connect(
            "/api/chat?vertexai=false&model=gemini-2.5-flash-native-audio-preview-12-2025&instruction=%20%20%20"
        ) as ws:
            _verify_standard_responses(ws)


def test_safe_b64encode_memoryview_and_bytearray():
    raw = b"smart glasses memoryview test"
    # Test bytearray
    b_arr = bytearray(raw)
    res1 = asyncio.run(_safe_b64encode(b_arr))
    assert base64.b64decode(res1) == raw

    # Test memoryview
    m_view = memoryview(raw)
    res2 = asyncio.run(_safe_b64encode(m_view))
    assert base64.b64decode(res2) == raw


def test_redact_sensitive_keys_advanced_patterns():
    text = (
        "Config client_secret=verySecretVal and private_key=privateRsaKey "
        "and header x-goog-api-key: gcpKeySecret and x-api-key: customSecretKey"
    )
    redacted = redact_sensitive_keys(text)
    assert "verySecretVal" not in redacted
    assert "privateRsaKey" not in redacted
    assert "gcpKeySecret" not in redacted
    assert "customSecretKey" not in redacted
    assert "[REDACTED]" in redacted


def test_http_security_headers_hsts_and_policies():
    response = client.get("/health")
    assert response.status_code == 200
    headers = response.headers
    assert "server" not in headers
    assert headers.get("strict-transport-security") == "max-age=31536000; includeSubDomains"
    assert headers.get("x-permitted-cross-domain-policies") == "none"
    assert headers.get("cross-origin-embedder-policy") == "require-corp"


def test_health_check_head_method():
    response = client.head("/health")
    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_redact_sensitive_keys_json_and_credentials():
    json_text = (
        '{"api_key": "secretValue123", "token": "mySecretToken456", '
        '"client_secret": "topSecretClientVal", "password": "superPassword999"} '
        "and auth Basic dXNlcjpwYXNzd29yZA== and credentials=credSecret888"
    )
    redacted = redact_sensitive_keys(json_text)
    assert "secretValue123" not in redacted
    assert "mySecretToken456" not in redacted
    assert "topSecretClientVal" not in redacted
    assert "superPassword999" not in redacted
    assert "dXNlcjpwYXNzd29yZA==" not in redacted
    assert "credSecret888" not in redacted
    assert '"api_key": "[REDACTED]"' in redacted
    assert '"token": "[REDACTED]"' in redacted
    assert '"client_secret": "[REDACTED]"' in redacted
    assert '"password": "[REDACTED]"' in redacted
    assert "Basic [REDACTED]" in redacted
    assert "credentials=[REDACTED]" in redacted


def test_redact_sensitive_keys_leading_query_and_headers():
    # Test credentials at start of string (no preceding whitespace/delimiter)
    assert redact_sensitive_keys("key=secretLeadingKey") == "key=[REDACTED]"
    assert redact_sensitive_keys("api_key=secretApiKey&other=1") == "api_key=[REDACTED]&other=1"
    assert redact_sensitive_keys("token=secretToken123") == "token=[REDACTED]"
    assert redact_sensitive_keys("Bearer secretBearerTokenVal") == "Bearer [REDACTED]"
    assert redact_sensitive_keys("Bearer secretBearer%2BToken%3D") == "Bearer [REDACTED]"
    assert redact_sensitive_keys("JWT secretJwtTokenVal") == "JWT [REDACTED]"
    assert redact_sensitive_keys("Digest secretDigestTokenVal") == "Digest [REDACTED]"
    assert redact_sensitive_keys("x-goog-api-key: secretGcpKey") == "x-goog-api-key: [REDACTED]"
    assert redact_sensitive_keys("x-api-key: secretCustomKey") == "x-api-key: [REDACTED]"


def test_mask_token_types():
    assert mask_token(None) == ""
    assert mask_token("") == ""
    assert mask_token("   ") == ""
    assert mask_token("\x00\x08") == ""
    assert mask_token("short") == "***"
    assert mask_token("12345678") == "***"
    assert mask_token("123456789") == "1234...6789"
    # Non-str input handling
    assert mask_token(1234567890) == "1234...7890"  # type: ignore


def test_extract_auth_token_control_characters_rejected():
    ws_mock = MagicMock()
    ws_mock.headers = {"x-api-key": "secret\x00key"}
    assert _extract_auth_token(ws_mock, None) is None

    ws_mock.headers = {"authorization": "Bearer token\x08with_ctrl"}
    assert _extract_auth_token(ws_mock, None) is None

    # C1 and Unicode bidi control characters
    ws_mock.headers = {"x-api-key": "secret\x85key"}
    assert _extract_auth_token(ws_mock, None) is None

    ws_mock.headers = {"authorization": "Bearer token\u202ewith_bidi"}
    assert _extract_auth_token(ws_mock, None) is None

    # Normal valid token
    ws_mock.headers = {"authorization": "Bearer valid_token_123"}
    assert _extract_auth_token(ws_mock, None) == "valid_token_123"


def test_allowed_origins_set_trailing_slash_and_case():
    orig = settings.ALLOWED_ORIGINS
    try:
        settings.ALLOWED_ORIGINS = "https://foo.com/, HTTPS://BAR.COM/ "
        assert settings.allowed_origins_set == {"https://foo.com", "https://bar.com"}
    finally:
        settings.ALLOWED_ORIGINS = orig


def test_clean_control_chars_helper():
    assert _clean_control_chars("") == ""
    assert _clean_control_chars(None) == ""
    raw = "Hello\x00\x08World\r\n\tTest\x1b[31m\x85Bidi\u202eAttack"
    cleaned = _clean_control_chars(raw)
    assert "\x00" not in cleaned
    assert "\x08" not in cleaned
    assert "\x1b" not in cleaned
    assert "\x85" not in cleaned
    assert "\u202e" not in cleaned
    assert "Hello" in cleaned
    assert "World" in cleaned
    assert "Attack" in cleaned


def test_is_truthy_flag():
    assert _is_truthy_flag(True) is True
    assert _is_truthy_flag(False) is False
    assert _is_truthy_flag(None) is False
    assert _is_truthy_flag("") is False
    assert _is_truthy_flag("non-empty") is True
    assert _is_truthy_flag(1) is True
    # MagicMock without explicit value should NOT evaluate to True
    mock_obj = MagicMock()
    assert _is_truthy_flag(mock_obj) is False


def test_normalize_params():
    assert _normalize_params("  foo  ", "", None, "  bar  ", 123) == ("foo", None, None, "bar", None)


def test_websocket_chat_origin_trailing_slash_handling():
    orig = settings.ALLOWED_ORIGINS
    settings.ALLOWED_ORIGINS = "https://trusted.com"
    try:
        # Origin with trailing slash should match normalized allowed origin
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/chat", headers={"origin": "https://trusted.com/"}) as ws,
        ):
            ws.receive_text()
        # Should pass origin check and fail later at config validation (1011) instead of 4003 (forbidden origin)
        assert excinfo.value.code == 1011
    finally:
        settings.ALLOWED_ORIGINS = orig


def test_websocket_chat_origin_control_chars_rejected():
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/chat", headers={"origin": "https://trusted.com\r\ninjected"}) as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 4003


def test_validate_raw_token_unit():
    assert _validate_raw_token(None) is None
    assert _validate_raw_token("") is None
    assert _validate_raw_token("   ") is None
    assert _validate_raw_token(12345) is None
    assert _validate_raw_token("valid_token") == "valid_token"
    assert _validate_raw_token("  valid_token_with_spaces  ") == "valid_token_with_spaces"
    assert _validate_raw_token("token\x00with_null") is None
    assert _validate_raw_token("token\r\nwith_crlf") is None
    assert _validate_raw_token("token\x85with_c1") is None
    assert _validate_raw_token("token\u202ewith_bidi") is None
    assert _validate_raw_token("a" * 4097) is None
    assert _validate_raw_token("a" * 4096) == "a" * 4096


def test_safe_b64decode_memoryview():
    raw = b"SmartGlassesGatewayMemoryviewTest"
    encoded = base64.b64encode(raw)
    mv = memoryview(encoded)
    decoded = asyncio.run(_safe_b64decode(mv))
    assert decoded == raw


def test_redact_sensitive_keys_expanded_secret_patterns():
    query = "?secret_key=superSecretKey123&session_token=sessionToken456&client-secret=myClientSec789"
    redacted_query = redact_sensitive_keys(query)
    assert "superSecretKey123" not in redacted_query
    assert "sessionToken456" not in redacted_query
    assert "myClientSec789" not in redacted_query
    assert "secret_key=[REDACTED]" in redacted_query
    assert "session_token=[REDACTED]" in redacted_query
    assert "client-secret=[REDACTED]" in redacted_query

    json_payload = '{"secret_key": "topSecret1", "session_token": "sessToken2", "private_key": "privKey3"}'
    redacted_json = redact_sensitive_keys(json_payload)
    assert "topSecret1" not in redacted_json
    assert "sessToken2" not in redacted_json
    assert "privKey3" not in redacted_json
    assert '"secret_key": "[REDACTED]"' in redacted_json
    assert '"session_token": "[REDACTED]"' in redacted_json
    assert '"private_key": "[REDACTED]"' in redacted_json


def test_validate_raw_token_bidi_and_bom_rejected():
    assert _validate_raw_token("token\u061cwith_alm") is None
    assert _validate_raw_token("token\u200ewith_lrm") is None
    assert _validate_raw_token("token\u200fwith_rlm") is None
    assert _validate_raw_token("token\ufeffwith_bom") is None


def test_extract_gemini_events_server_content_transcription():
    # Verify fallback to server_content when top-level transcription is missing
    resp_dict = {
        "server_content": {
            "input_transcription": {"text": "サーバー側の入力音声"},
            "output_transcription": {"text": "サーバー側の出力音声"},
        }
    }
    events = asyncio.run(extract_gemini_events(resp_dict))
    assert events == [
        {"type": "user_text", "data": "サーバー側の入力音声"},
        {"type": "text", "data": "サーバー側の出力音声"},
    ]


def test_get_int_env_max_val_boundary(monkeypatch):
    from backend.app.config import _get_int_env

    # Within bounds
    assert _get_int_env("TEST_INT_VAL", 100, min_val=10, max_val=500) == 100
    # Exceeds max_val -> fallback to default
    monkeypatch.setenv("TEST_INT_BOUND", "600")
    assert _get_int_env("TEST_INT_BOUND", 100, min_val=10, max_val=500) == 100
    # Below min_val -> fallback to default
    monkeypatch.setenv("TEST_INT_BOUND", "5")
    assert _get_int_env("TEST_INT_BOUND", 100, min_val=10, max_val=500) == 100
    # Within bounds -> parsed value
    monkeypatch.setenv("TEST_INT_BOUND", "250")
    assert _get_int_env("TEST_INT_BOUND", 100, min_val=10, max_val=500) == 250


def test_security_headers_middleware_comprehensive():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("surrogate-control") == "no-store"
    assert response.headers.get("cross-origin-embedder-policy") == "require-corp"


def test_websocket_chat_origin_oversized_rejected():
    oversized_origin = "https://trusted.com/" + "a" * 2040
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/chat", headers={"origin": oversized_origin}) as ws,
    ):
        ws.receive_text()
    assert excinfo.value.code == 4003


def test_redact_sensitive_keys_passcode_and_app_secret():
    text = (
        "?app_secret=myAppSecret123&passcode=987654&secret_token=topSecretToken "
        'and JSON {"app_secret": "myAppSecret456", "passcode": "secretPasscode123", "secret_token": "tokenAbc"}'
    )
    redacted = redact_sensitive_keys(text)
    assert "myAppSecret123" not in redacted
    assert "987654" not in redacted
    assert "topSecretToken" not in redacted
    assert "myAppSecret456" not in redacted
    assert "secretPasscode123" not in redacted
    assert "tokenAbc" not in redacted
    assert "app_secret=[REDACTED]" in redacted
    assert "passcode=[REDACTED]" in redacted
    assert "secret_token=[REDACTED]" in redacted
    assert '"app_secret": "[REDACTED]"' in redacted
    assert '"passcode": "[REDACTED]"' in redacted
    assert '"secret_token": "[REDACTED]"' in redacted


def test_b64decode_bytes_helper_and_safe_b64encode():
    raw = b"Consolidated base64 helpers test data"
    encoded_str = asyncio.run(_safe_b64encode(raw))
    decoded = _b64decode_bytes(encoded_str.encode("ascii"))
    assert decoded == raw

    # Test bytearray and memoryview inputs to _b64decode_bytes
    assert _b64decode_bytes(bytearray(encoded_str.encode("ascii"))) == raw
    assert _b64decode_bytes(memoryview(encoded_str.encode("ascii"))) == raw


def test_decode_and_send_blob_memoryview():
    raw_audio = b"\x00\x01\x02\x03" * 10
    encoded = base64.b64encode(raw_audio)
    mv = memoryview(encoded)

    mock_conn = MagicMock()
    mock_session = AsyncMock()
    mock_conn._gemini_session = mock_session

    asyncio.run(decode_and_send_blob(mock_conn, mv, "audio"))
    mock_session.send_realtime_input.assert_called_once()
    blob_arg = mock_session.send_realtime_input.call_args[1]["audio"]
    assert blob_arg.data == raw_audio
    assert blob_arg.mime_type == "audio/pcm;rate=16000"


def test_redact_sensitive_keys_access_key_and_bearer_token():
    text = (
        "?access_key=secretKey123&api_access_key=myApiAccessKey&secret_access_key=mySecretAccessKey&bearer_token=myBearerToken "
        'and JSON {"access_key": "secretKey456", "api_access_key": "myApiAccessKey2", '
        '"secret_access_key": "mySecretAccessKey2", "bearer_token": "myBearerToken2"}'
    )
    redacted = redact_sensitive_keys(text)
    assert "secretKey123" not in redacted
    assert "myApiAccessKey" not in redacted
    assert "mySecretAccessKey" not in redacted
    assert "myBearerToken" not in redacted
    assert "secretKey456" not in redacted
    assert "myApiAccessKey2" not in redacted
    assert "mySecretAccessKey2" not in redacted
    assert "myBearerToken2" not in redacted
    assert "access_key=[REDACTED]" in redacted
    assert "api_access_key=[REDACTED]" in redacted
    assert "secret_access_key=[REDACTED]" in redacted
    assert "bearer_token=[REDACTED]" in redacted
    assert '"access_key": "[REDACTED]"' in redacted
    assert '"api_access_key": "[REDACTED]"' in redacted
    assert '"secret_access_key": "[REDACTED]"' in redacted
    assert '"bearer_token": "[REDACTED]"' in redacted


def test_normalize_b64_bytes_helper():
    raw = b"Hello, World!"
    b64_bytes = base64.b64encode(raw)
    b64_str = b64_bytes.decode("ascii")

    # String input with surrounding whitespace
    assert _normalize_b64_bytes(f"  {b64_str}  \n") == b64_bytes
    # Bytes input with surrounding whitespace
    assert _normalize_b64_bytes(b"  " + b64_bytes + b"  \r\n") == b64_bytes
    # Bytearray input
    assert _normalize_b64_bytes(bytearray(b64_bytes)) == b64_bytes
    # Memoryview input
    assert _normalize_b64_bytes(memoryview(b64_bytes)) == b64_bytes


def test_has_dangerous_control_chars_helper():
    assert not _has_dangerous_control_chars("Normal text with standard content.")
    assert not _has_dangerous_control_chars("Text with\nnewlines\tand\rtabs.")
    assert _has_dangerous_control_chars("Text with null byte \x00.")
    assert _has_dangerous_control_chars("Text with bell \x07.")
    assert _has_dangerous_control_chars("Text with Trojan Source Bidi \u202e text.")


def test_clean_control_chars_optimization():
    # Regular text without control chars should return stripped text directly
    assert _clean_control_chars("  Clean text without control chars  ") == "Clean text without control chars"
    # Text with control chars should replace control chars with space and strip
    assert _clean_control_chars("Control\x00char\x1ftest") == "Control char test"
    # Empty and None inputs
    assert _clean_control_chars("") == ""
    assert _clean_control_chars(None) == ""


def test_security_headers_strips_x_powered_by():
    # Verify that X-Powered-By is removed if injected into response
    @app.get("/test-custom-headers")
    async def custom_header_route():
        from fastapi import Response

        resp = Response(content="ok")
        resp.headers["X-Powered-By"] = "TestFramework/1.0"
        resp.headers["Server"] = "TestServer/1.0"
        return resp

    resp = client.get("/test-custom-headers")
    assert resp.status_code == 200
    assert "x-powered-by" not in resp.headers
    assert "server" not in resp.headers
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Cache-Control") == "no-store"


def test_stream_exception_classes():
    assert RuntimeError in STREAM_EXCEPTION_CLASSES
    assert OSError in STREAM_EXCEPTION_CLASSES
    assert WebSocketDisconnect in STREAM_EXCEPTION_CLASSES
    assert ConnectionResetError in STREAM_EXCEPTION_CLASSES
    assert BrokenPipeError in STREAM_EXCEPTION_CLASSES
    assert asyncio.IncompleteReadError in STREAM_EXCEPTION_CLASSES
    try:
        import anyio

        assert anyio.EndOfStream in STREAM_EXCEPTION_CLASSES
    except ImportError:
        pass


def test_decode_and_send_blob_non_ascii_unicode():
    mock_conn = MagicMock()
    # Non-ASCII unicode text should not cause an uncaught UnicodeEncodeError
    asyncio.run(decode_and_send_blob(mock_conn, "日本語テスト音声データ", "audio"))
    mock_conn._gemini_session.send_realtime_input.assert_not_called()


def test_extract_auth_token_x_access_key():
    mock_ws = MagicMock()
    mock_ws.headers = {"x-access-key": "access_key_test_12345"}
    token = _extract_auth_token(mock_ws, None)
    assert token == "access_key_test_12345"
    assert "x-access-key" in AUTH_HEADER_NAMES


def test_redact_sensitive_keys_extended():
    # Test query param redaction with api_secret, oauth_token, client_key, session_id, x-access-key
    query = "?api_secret=shhh123&oauth_token=tok456&client_key=key789&session_id=sess_abc&x-access-key=acc_xyz"
    redacted_query = redact_sensitive_keys(query)
    assert "shhh123" not in redacted_query
    assert "tok456" not in redacted_query
    assert "key789" not in redacted_query
    assert "sess_abc" not in redacted_query
    assert "acc_xyz" not in redacted_query
    assert "api_secret=[REDACTED]" in redacted_query
    assert "oauth_token=[REDACTED]" in redacted_query
    assert "client_key=[REDACTED]" in redacted_query
    assert "session_id=[REDACTED]" in redacted_query
    assert "x-access-key=[REDACTED]" in redacted_query

    # Test JSON redaction with api_secret, oauth_token, client_key, session_id
    json_str = '{"api_secret": "secret123", "oauth_token": "oauth456", "client_key": "ck789", "session_id": "sid012"}'
    redacted_json = redact_sensitive_keys(json_str)
    assert "secret123" not in redacted_json
    assert "oauth456" not in redacted_json
    assert "ck789" not in redacted_json
    assert "sid012" not in redacted_json
    assert '"api_secret": "[REDACTED]"' in redacted_json
    assert '"oauth_token": "[REDACTED]"' in redacted_json
    assert '"client_key": "[REDACTED]"' in redacted_json
    assert '"session_id": "[REDACTED]"' in redacted_json

    # Test Header redaction with x-access-key
    header_str = "x-access-key: my_super_secret_token_12345"
    redacted_header = redact_sensitive_keys(header_str)
    assert "my_super_secret_token_12345" not in redacted_header
    assert "x-access-key: [REDACTED]" in redacted_header


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__]))
