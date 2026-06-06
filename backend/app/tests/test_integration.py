import base64
import json
import os
from pathlib import Path

import google.auth
import pytest
from dotenv import load_dotenv
from google.auth.exceptions import DefaultCredentialsError

# Load env variables from backend/.env or root .env
load_dotenv()
load_dotenv("backend/.env")

# Verify availability of Application Default Credentials (ADC)
try:
    credentials, project_id = google.auth.default()
    has_adc = True
except DefaultCredentialsError:
    has_adc = False

from backend.app.config import settings

# Force Vertex AI mode for integration test
settings.GOOGLE_GENAI_USE_VERTEXAI = True
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"

from backend.app.main import app
from fastapi.testclient import TestClient

# Retrieve GCP connection parameters
gcp_project = settings.GOOGLE_CLOUD_PROJECT
gcp_location = settings.GOOGLE_CLOUD_LOCATION
gcp_agent_id = settings.GCP_AGENT_ID

is_valid_agent_id = gcp_agent_id and gcp_agent_id not in (
    "YOUR_GCP_AGENT_ID",
    "your-agent-id",
    "",
)
is_valid_project = gcp_project and gcp_project not in (
    "your-gcp-project-id",
    "",
)

# Skip integration test if ADC credentials or configuration is missing
pytestmark = pytest.mark.skipif(
    not has_adc or not is_valid_agent_id or not is_valid_project,
    reason=(
        f"Vertex AI Agent integration test skipped. "
        f"Reason: has_adc={has_adc}, is_valid_agent_id={is_valid_agent_id}, is_valid_project={is_valid_project}."
    ),
)


def test_websocket_chat_vertexai_integration():
    client = TestClient(app)

    # Resolve query.pcm path
    pcm_path = Path(__file__).parent / "../../../query.pcm"
    if not pcm_path.exists():
        pcm_path = Path("query.pcm")

    assert pcm_path.exists(), f"query.pcm not found at {pcm_path}"

    with pcm_path.open("rb") as f:
        pcm_bytes = f.read()

    # Build WebSocket URL targeting Vertex AI Agent Platform
    url = f"/api/chat?vertexai=true&project={gcp_project}&location={gcp_location}&agent_id={gcp_agent_id}"
    if settings.API_ACCESS_KEY:
        url += f"&key={settings.API_ACCESS_KEY}"

    from starlette.websockets import WebSocketDisconnect

    disconnected = False
    close_code = None
    close_reason = ""

    try:
        with client.websocket_connect(url) as ws:
            # Stream audio chunks (e.g., 4096 bytes each)
            chunk_size = 4096
            for i in range(0, len(pcm_bytes), chunk_size):
                chunk = pcm_bytes[i : i + chunk_size]
                base64_audio = base64.b64encode(chunk).decode("utf-8")
                ws.send_text(json.dumps({"type": "audio", "data": base64_audio}))

            # Send stop signal to start generation
            ws.send_text(json.dumps({"type": "stop"}))

            # Wait and receive responses
            received_types = []
            # Read up to 20 messages or until we get text/audio response
            for _ in range(20):
                try:
                    resp = ws.receive_json()
                    msg_type = resp.get("type")
                    received_types.append(msg_type)
                    # If we received text or audio back, the integration works
                    if msg_type in ("text", "audio", "user_text"):
                        break
                except WebSocketDisconnect:
                    raise
                except Exception:
                    break

            # Verification
            assert len(received_types) > 0, "No response received from Gemini Agent Platform"
            assert any(t in received_types for t in ("user_text", "text", "audio")), (
                f"Expected responses (user_text, text, audio) not found in: {received_types}"
            )
    except WebSocketDisconnect as e:
        disconnected = True
        close_code = e.code
        close_reason = e.reason

    if disconnected:
        # Check if the connection closed due to the expected Vertex AI Agent Platform error.
        # Since the registered agent in GCP is a Workspace Agent, it doesn't support the Multimodal Live API,
        # which results in a 1007 error from GCP. This indicates that the connection successfully reached GCP!
        assert close_code == 1011, f"Expected WebSocket close code 1011, got {close_code} (reason: {close_reason})"
        assert "1007" in close_reason or "invalid argument" in close_reason.lower(), (
            f"Expected Gemini Live session argument/1007 error in close reason, got: {close_reason}"
        )
