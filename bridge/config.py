"""Configuration for OpenClaw Bridge."""

import os

# OpenClaw WebSocket gateway URL
OPENCLAW_WS_URL = os.getenv("OPENCLAW_WS_URL", "ws://127.0.0.1:18789")

# Bridge server settings
BRIDGE_HOST = os.getenv("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8081"))

# Secret token for verifying requests from Omi OpenClaw App
# This token is generated when you configure the app and shown once
# Set it here to verify incoming requests
OMI_SECRET_TOKEN = os.getenv("OMI_SECRET_TOKEN", "")

# Request timeout for OpenClaw (in seconds)
OPENCLAW_TIMEOUT = float(os.getenv("OPENCLAW_TIMEOUT", "120.0"))
