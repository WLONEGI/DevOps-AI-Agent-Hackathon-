import asyncio
import base64
import json
import warnings
from unittest.mock import MagicMock, patch

# Ignore Google GenAI SDK's internal deprecation warning under Python 3.14+
warnings.filterwarnings("ignore", category=DeprecationWarning, module="google.genai")

from backend.server import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health_check():
    # 1. Health Check Test
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# Mock Live Session for WebSocket testing
class MockLiveSession:
    def __init__(self, *args, **kwargs):
        self.sent_inputs = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def send_realtime_input(self, *args, **kwargs):
        self.sent_inputs.append((args, kwargs))

    async def receive(self):
        # 1. Normal response (audio and transcription)
        mock_response1 = MagicMock()
        mock_response1.server_content = MagicMock()
        mock_response1.server_content.interrupted = False
        part = MagicMock()
        part.inline_data = MagicMock(data=b"fakeaudio")
        mock_response1.server_content.model_turn = MagicMock(parts=[part])
        mock_response1.server_content.output_transcription = MagicMock(text="テスト応答")
        mock_response1.server_content.input_transcription = MagicMock(text="ユーザー発話")
        mock_response1.session_resumption_update = None
        mock_response1.go_away = None
        yield mock_response1

        # 2. Interrupted signal
        mock_response2 = MagicMock()
        mock_response2.server_content = MagicMock()
        mock_response2.server_content.interrupted = True
        mock_response2.server_content.model_turn = None
        mock_response2.server_content.output_transcription = None
        mock_response2.server_content.input_transcription = None
        mock_response2.session_resumption_update = None
        mock_response2.go_away = None
        yield mock_response2

        # 3. Session Resumption Update
        mock_response3 = MagicMock()
        mock_response3.server_content = None
        sru = MagicMock()
        sru.new_handle = "new_fake_handle"
        sru.resumable = True
        sru.last_consumed_client_message_index = 42
        mock_response3.session_resumption_update = sru
        mock_response3.go_away = None
        yield mock_response3

        # 4. GoAway signal
        mock_response4 = MagicMock()
        mock_response4.server_content = None
        mock_response4.session_resumption_update = None
        ga = MagicMock()
        ga.time_left = "5s"
        mock_response4.go_away = ga
        yield mock_response4

        # Keep connection open for client to disconnect
        while True:
            await asyncio.sleep(0.1)


@patch("backend.server.get_gemini_client")
def test_websocket_chat(mock_get_client):
    # Setup mock Gemini client
    mock_client = MagicMock()
    mock_session = MockLiveSession()

    # Mocking client.aio.live.connect async context manager
    mock_client.aio.live.connect = MagicMock(return_value=mock_session)
    mock_get_client.return_value = mock_client

    with client.websocket_connect("/api/chat?key=testkey&model=test-model&voice=test-voice") as ws:
        # Send text image message
        ws.send_text(json.dumps({"type": "image", "data": base64.b64encode(b"fakeimage").decode("utf-8")}))

        # Send text audio message
        ws.send_text(json.dumps({"type": "audio", "data": base64.b64encode(b"fakeaudio").decode("utf-8")}))

        # Send stop message
        ws.send_text(json.dumps({"type": "stop"}))

        # Receive mock responses from WebSocket
        # mock_response1 yields:
        # - audio
        # - text
        # - user_text
        received_types = []
        for _ in range(3):
            resp = ws.receive_json()
            received_types.append(resp.get("type"))

        assert "audio" in received_types
        assert "text" in received_types
        assert "user_text" in received_types

        # mock_response2: interrupt
        resp = ws.receive_json()
        assert resp.get("type") == "interrupt"

        # mock_response3: resumption_token
        resp = ws.receive_json()
        assert resp.get("type") == "resumption_token"
        assert resp.get("data").get("handle") == "new_fake_handle"
        assert resp.get("data").get("resumable") is True
        assert resp.get("data").get("last_consumed_client_message_index") == 42

        # mock_response4: go_away
        resp = ws.receive_json()
        assert resp.get("type") == "go_away"
        assert resp.get("data").get("time_left") == "5s"

        # Verify query parameters were used to configure Gemini client
        mock_client.aio.live.connect.assert_called_once()
        called_args, called_kwargs = mock_client.aio.live.connect.call_args
        assert called_kwargs.get("model") == "test-model"

        # Verify sent inputs were received by the mock session
        assert len(mock_session.sent_inputs) >= 2


@patch("backend.server.get_gemini_client")
def test_websocket_chat_resumption(mock_get_client):
    # Setup mock Gemini client with resumption token
    mock_client = MagicMock()
    mock_session = MockLiveSession()
    mock_client.aio.live.connect = MagicMock(return_value=mock_session)
    mock_get_client.return_value = mock_client

    with client.websocket_connect("/api/chat?key=testkey&resumption_token=old_fake_handle"):
        # Verify query parameters were used to configure Gemini client with resumption token
        mock_client.aio.live.connect.assert_called_once()
        called_args, called_kwargs = mock_client.aio.live.connect.call_args
        config = called_kwargs.get("config")
        assert config.session_resumption.handle == "old_fake_handle"


@patch("backend.server.get_gemini_client")
def test_websocket_chat_vertexai_agent(mock_get_client):
    # Setup mock Gemini client
    mock_client = MagicMock()
    mock_session = MockLiveSession()
    mock_client.aio.live.connect = MagicMock(return_value=mock_session)
    mock_get_client.return_value = mock_client

    with client.websocket_connect("/api/chat?vertexai=true&project=my-project&location=us-central1&agent_id=my-agent"):
        # Verify get_gemini_client was called with vertexai and project details
        mock_get_client.assert_called_once_with(
            api_key=None,
            vertexai=True,
            project="my-project",
            location="us-central1",
        )

        # Verify live.connect was called with the correct Agent resource path
        mock_client.aio.live.connect.assert_called_once()
        called_args, called_kwargs = mock_client.aio.live.connect.call_args
        assert called_kwargs.get("model") == "projects/my-project/locations/us-central1/agents/my-agent"

        # Verify that system_instruction and speech_config are NOT set by default in config (prioritizing agent studio settings)
        config = called_kwargs.get("config")
        assert not hasattr(config, "system_instruction") or config.system_instruction is None
        assert not hasattr(config, "speech_config") or config.speech_config is None


@patch("backend.server.genai.Client")
def test_get_gemini_client_vertexai(mock_genai_client):
    from backend.server import get_gemini_client

    # Test standard Client initialization
    get_gemini_client(api_key="my-api-key")
    mock_genai_client.assert_called_with(api_key="my-api-key")

    # Test Vertex AI client initialization
    get_gemini_client(vertexai=True, project="my-project", location="us-central1")
    mock_genai_client.assert_called_with(vertexai=True, project="my-project", location="us-central1")


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__]))
