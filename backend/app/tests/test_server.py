import base64
import json
from unittest.mock import MagicMock, patch

from backend.app.config import settings

# Force default test configurations to isolate tests from local .env settings
settings.GOOGLE_GENAI_USE_VERTEXAI = False
settings.GCP_AGENT_ID = None

from backend.app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health_check():
    # 1. Health Check Test
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_websocket_chat_missing_config():
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect("/api/chat") as ws:
        ws.receive_text()
    assert excinfo.value.code == 1011


def test_websocket_chat_unauthorized():
    import pytest
    from starlette.websockets import WebSocketDisconnect

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
    import asyncio

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

    # Mock send_realtime
    async def mock_send_realtime(*args, **kwargs):
        pass

    mock_connection.send_realtime = MagicMock(side_effect=mock_send_realtime)

    # Mock LlmResponse objects
    mock_resp1 = MagicMock()
    mock_resp1.interrupted = False
    mock_resp1.live_session_resumption_update = None
    mock_resp1.go_away = None
    mock_resp1.input_transcription = MagicMock(text="ユーザー発話")
    mock_resp1.content = None

    mock_resp2 = MagicMock()
    mock_resp2.interrupted = False
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


@patch("backend.app.main.ADKGemini")
def test_websocket_chat_vertexai_agent(mock_gemini_class):
    setup_mock_gemini_connection(mock_gemini_class)

    with client.websocket_connect(
        "/api/chat?vertexai=true&project=my-project&location=us-central1&agent_id=my-agent"
    ) as ws:
        # Send text image message
        ws.send_text(json.dumps({"type": "image", "data": base64.b64encode(b"fakeimage").decode("utf-8")}))

        # Send text audio message
        ws.send_text(json.dumps({"type": "audio", "data": base64.b64encode(b"fakeaudio").decode("utf-8")}))

        # Send stop message
        ws.send_text(json.dumps({"type": "stop"}))

        # Receive mock responses from WebSocket
        received_types = []
        for _ in range(3):
            resp = ws.receive_json()
            received_types.append(resp.get("type"))

        assert "audio" in received_types
        assert "text" in received_types
        assert "user_text" in received_types

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
        # Send text image message
        ws.send_text(json.dumps({"type": "image", "data": base64.b64encode(b"fakeimage").decode("utf-8")}))

        # Send text audio message
        ws.send_text(json.dumps({"type": "audio", "data": base64.b64encode(b"fakeaudio").decode("utf-8")}))

        # Send stop message
        ws.send_text(json.dumps({"type": "stop"}))

        # Receive mock responses from WebSocket
        received_types = []
        for _ in range(3):
            resp = ws.receive_json()
            received_types.append(resp.get("type"))

        assert "audio" in received_types
        assert "text" in received_types
        assert "user_text" in received_types


@patch("backend.app.main.ADKGemini")
def test_websocket_chat_vertexai_direct_model_with_params(mock_gemini_class):
    setup_mock_gemini_connection(mock_gemini_class)

    with client.websocket_connect(
        "/api/chat?vertexai=true&model=publishers/google/models/gemini-2.0-flash-exp&project=my-project&location=us-east1"
    ) as ws:
        # Send text image message
        ws.send_text(json.dumps({"type": "image", "data": base64.b64encode(b"fakeimage").decode("utf-8")}))

        # Send text audio message
        ws.send_text(json.dumps({"type": "audio", "data": base64.b64encode(b"fakeaudio").decode("utf-8")}))

        # Send stop message
        ws.send_text(json.dumps({"type": "stop"}))

        # Receive mock responses from WebSocket
        received_types = []
        for _ in range(3):
            resp = ws.receive_json()
            received_types.append(resp.get("type"))

        assert "audio" in received_types
        assert "text" in received_types
        assert "user_text" in received_types

    mock_gemini_class.assert_called_once_with(
        model="publishers/google/models/gemini-2.0-flash-exp",
        use_vertexai_flag=True,
        project="my-project",
        location="us-east1",
    )


@patch("backend.app.main.ADKGemini")
def test_websocket_chat_standard_gemini(mock_gemini_class):
    mock_connection = setup_mock_gemini_connection(mock_gemini_class)

    with client.websocket_connect("/api/chat?vertexai=false&model=gemini-2.5-flash-native-audio-preview-12-2025") as ws:
        # Send text image message
        ws.send_text(json.dumps({"type": "image", "data": base64.b64encode(b"fakeimage").decode("utf-8")}))

        # Send text audio message
        ws.send_text(json.dumps({"type": "audio", "data": base64.b64encode(b"fakeaudio").decode("utf-8")}))

        # Send stop message
        ws.send_text(json.dumps({"type": "stop"}))

        # Receive mock responses from WebSocket
        received_types = []
        for _ in range(3):
            resp = ws.receive_json()
            received_types.append(resp.get("type"))

        assert "audio" in received_types
        assert "text" in received_types
        assert "user_text" in received_types

        # Verify connection.send_realtime_input was called
        mock_connection._gemini_session.send_realtime_input.assert_called()


def test_websocket_chat_invalid_parameters():
    import pytest
    from starlette.websockets import WebSocketDisconnect

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
    from backend.app.gemini import create_live_connect_config
    from google.genai import types

    config = create_live_connect_config(
        use_vertexai=False,
        gcp_agent_id=None,
        voice_name="Puck",
        resumption_token=None,
    )
    assert config.realtime_input_config is not None
    assert config.realtime_input_config.activity_handling == types.ActivityHandling.NO_INTERRUPTION


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__]))
