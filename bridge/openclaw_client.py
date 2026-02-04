"""WebSocket client for communicating with OpenClaw Gateway."""

import asyncio
import json
import logging
import os
import uuid

from websockets import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import OPENCLAW_TIMEOUT

logger = logging.getLogger(__name__)

# OpenClaw Gateway WebSocket URL
OPENCLAW_WS_URL = os.getenv("OPENCLAW_WS_URL", "ws://127.0.0.1:18789")
# Gateway auth token (from gateway.auth.token in OpenClaw config or OPENCLAW_GATEWAY_TOKEN env var)
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")


class OpenClawClient:
    """
    WebSocket client for communicating with OpenClaw Gateway.

    Connects as an operator and uses chat.send to send messages.
    """

    def __init__(self, url: str = OPENCLAW_WS_URL, token: str = OPENCLAW_GATEWAY_TOKEN):
        self.url = url
        self.token = token
        self.ws = None
        self._lock = asyncio.Lock()
        self._connected = False

    async def connect(self):
        """Establish WebSocket connection and complete handshake."""
        # Check if already connected - use open attribute (newer websockets) or closed (older)
        if self.ws is not None and self._connected:
            try:
                # Check if connection is still open
                if hasattr(self.ws, 'open'):
                    if self.ws.open:
                        return
                elif hasattr(self.ws, 'closed'):
                    if not self.ws.closed:
                        return
                else:
                    # Fallback: try to check state
                    return
            except Exception:
                pass  # Connection check failed, reconnect

        logger.info(f"Connecting to OpenClaw at {self.url}")
        self.ws = await connect(self.url)
        logger.info("WebSocket connected, waiting for challenge...")

        # Wait for connect.challenge
        challenge_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
        challenge = json.loads(challenge_text)

        if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
            raise Exception(f"Expected connect.challenge, got: {challenge}")

        nonce = challenge.get("payload", {}).get("nonce", "")
        logger.info(f"Received challenge with nonce: {nonce[:20]}...")

        # Send connect request
        # For local loopback, device signing is not required
        connect_request = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "connect",
            "params": {
                "minProtocol": 3,
                "maxProtocol": 3,
                "client": {
                    "id": "cli",
                    "version": "1.0.0",
                    "platform": "linux",
                    "mode": "cli",
                },
                "role": "operator",
                "scopes": ["operator.read", "operator.write"],
                "caps": [],
                "commands": [],
                "permissions": {},
                "locale": "en-US",
                "userAgent": "omi-openclaw-bridge/1.0.0",
            },
        }

        # Add auth token if configured
        if self.token:
            connect_request["params"]["auth"] = {"token": self.token}

        await self.ws.send(json.dumps(connect_request))
        logger.info("Sent connect request")

        # Wait for response
        response_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
        response = json.loads(response_text)
        logger.info(f"Connect response: {json.dumps(response)[:200]}...")

        if response.get("type") == "res" and response.get("ok"):
            logger.info("Successfully connected to OpenClaw Gateway")
            self._connected = True
        else:
            error = response.get("error", {})
            error_msg = error.get("message", str(response))
            raise Exception(f"Connect failed: {error_msg}")

    async def disconnect(self):
        """Close the WebSocket connection."""
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass  # Already closed or error closing
        self.ws = None
        self._connected = False
        logger.info("Disconnected from OpenClaw")

    async def send_message(self, message: str, timeout: float = OPENCLAW_TIMEOUT) -> str:
        """
        Send a chat message to OpenClaw and wait for response.

        Args:
            message: The message to send
            timeout: Maximum time to wait for response (seconds)

        Returns:
            The response text from OpenClaw
        """
        async with self._lock:
            try:
                await self.connect()

                # Generate unique IDs for this request
                request_id = str(uuid.uuid4())
                idempotency_key = str(uuid.uuid4())

                # Send chat.send request
                chat_request = {
                    "type": "req",
                    "id": request_id,
                    "method": "chat.send",
                    "params": {
                        "sessionKey": "main",  # Default session
                        "message": message,
                        "idempotencyKey": idempotency_key,
                    },
                }

                logger.info(f"Sending chat.send: {message[:100]}...")
                await self.ws.send(json.dumps(chat_request))

                # chat.send returns immediately with { runId, status: "started" }
                # Then we need to listen for chat events until completion
                ack_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                ack = json.loads(ack_text)
                logger.info(f"chat.send ack: {json.dumps(ack)[:200]}...")

                if ack.get("type") == "res" and not ack.get("ok"):
                    error = ack.get("error", {})
                    return f"Error: {error.get('message', str(ack))}"

                # Now listen for chat events until we get the final response
                # Events have format: { type: "event", event: "chat", payload: { runId, sessionKey, seq, state, message } }
                response_content = ""
                end_time = asyncio.get_event_loop().time() + timeout

                while True:
                    remaining = end_time - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()

                    event_text = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
                    event = json.loads(event_text)

                    event_type = event.get("type")
                    event_name = event.get("event", "")
                    payload = event.get("payload", {})

                    logger.info(
                        f"Received: type={event_type}, event={event_name}, payload keys={list(payload.keys()) if isinstance(payload, dict) else 'N/A'}"
                    )

                    if event_type == "event" and event_name == "chat":
                        # Chat event - check state
                        state = payload.get("state", "")
                        message = payload.get("message", {})
                        text = payload.get("text", "")

                        logger.info(f"Chat event: state={state}, message={str(message)[:200]}")

                        # Accumulate text chunks
                        if text:
                            response_content += text

                        # Extract content from message if present
                        # Each delta contains the full message so far, so we replace (not append)
                        if isinstance(message, dict) and message.get("content"):
                            content = message.get("content")
                            if isinstance(content, str):
                                response_content = content
                            elif isinstance(content, list):
                                # Content is an array of content blocks - extract all text
                                extracted = ""
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        extracted += block.get("text", "")
                                if extracted:
                                    response_content = extracted

                        # Check if this is the final message
                        if state == "final" or state == "done":
                            break
                        elif state == "error":
                            error_msg = payload.get("error", "Unknown error")
                            return f"Error: {error_msg}"

                    # Continue listening for more events

                return response_content or "No response received"

            except asyncio.TimeoutError:
                logger.error(f"OpenClaw request timed out after {timeout}s")
                raise Exception(f"Request timed out after {timeout} seconds")

            except ConnectionClosed as e:
                logger.error(f"Connection closed: {e}")
                self.ws = None
                self._connected = False
                raise Exception(f"Connection closed: {e}")

            except WebSocketException as e:
                logger.error(f"WebSocket error: {e}")
                self.ws = None
                self._connected = False
                raise Exception(f"WebSocket error: {e}")

    async def health_check(self) -> bool:
        """Check if OpenClaw is reachable."""
        try:
            await self.connect()
            return True
        except Exception as e:
            logger.warning(f"Health check failed: {e}")
            return False


# Global client instance
openclaw_client = OpenClawClient()
