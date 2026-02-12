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

# OpenClaw session key to send messages to
OPENCLAW_SESSION_KEY = os.getenv("OPENCLAW_SESSION_KEY", "agent:main:main")

# Request timeout for OpenClaw (in seconds)
OPENCLAW_TIMEOUT = float(os.getenv("OPENCLAW_TIMEOUT", "120.0"))

# Threshold for switching to background mode (in seconds)
# If OpenClaw doesn't respond with a final answer within this time,
# return acknowledgment to user and continue waiting in background,
# then send result via callback when complete
QUICK_RESPONSE_TIMEOUT = float(os.getenv("QUICK_RESPONSE_TIMEOUT", "110.0"))
