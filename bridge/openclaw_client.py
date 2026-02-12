"""WebSocket client for communicating with OpenClaw Gateway."""

import asyncio
import json
import logging
import os
import uuid
from typing import Callable, Optional

from websockets import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .config import OPENCLAW_SESSION_KEY, OPENCLAW_TIMEOUT, QUICK_RESPONSE_TIMEOUT

logger = logging.getLogger(__name__)

# OpenClaw Gateway WebSocket URL
OPENCLAW_WS_URL = os.getenv("OPENCLAW_WS_URL", "ws://127.0.0.1:18789")
# Gateway auth token (from gateway.auth.token in OpenClaw config or OPENCLAW_GATEWAY_TOKEN env var)
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")

# How long to keep listening for follow-up results after a response is returned.
# If no events arrive for this duration, the follow-up listener stops.
FOLLOWUP_IDLE_TIMEOUT = float(os.getenv("FOLLOWUP_IDLE_TIMEOUT", "180.0"))


def _extract_response_content(event: dict, current_content: str) -> str:
    """
    Extract response content from an OpenClaw event.

    Handles both 'agent' events (stream=assistant) and 'chat' events.
    """
    event_type = event.get("type")
    event_name = event.get("event", "")
    payload = event.get("payload", {})

    # Capture text from agent events with stream="assistant"
    if event_type == "event" and event_name == "agent":
        stream = payload.get("stream", "")
        data = payload.get("data", {})
        if stream == "assistant" and isinstance(data, dict) and data.get("text"):
            return data.get("text", "")

    if event_type == "event" and event_name == "chat":
        message_obj = payload.get("message", {})
        text = payload.get("text", "")

        if text:
            current_content += text

        # Each delta contains the full message so far, so we replace
        if isinstance(message_obj, dict) and message_obj.get("content"):
            content = message_obj.get("content")
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                extracted = ""
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        extracted += block.get("text", "")
                if extracted:
                    return extracted

    return current_content


def _check_terminal_state(event: dict) -> tuple[str, bool]:
    """
    Check if an event represents a terminal state.

    Returns:
        Tuple of (state, has_pending_tools):
        - state: "final", "aborted", "error", or "" if not terminal
        - has_pending_tools: True if terminal but has pending tool calls
    """
    event_type = event.get("type")
    event_name = event.get("event", "")
    payload = event.get("payload", {})

    if event_type != "event" or event_name != "chat":
        return "", False

    state = payload.get("state", "")

    if state in ("final", "done", "complete"):
        message_obj = payload.get("message", {})
        has_pending_tools = False
        if isinstance(message_obj, dict) and message_obj.get("content"):
            content = message_obj.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        has_pending_tools = True
                        break
        return "final", has_pending_tools

    if state == "aborted":
        return "aborted", False

    if state == "error":
        return "error", False

    return "", False


# Type for the callback function that sends follow-up results
FollowupCallback = Callable[[str], asyncio.coroutines]


class OpenClawClient:
    """
    WebSocket client for communicating with OpenClaw Gateway.

    Maintains a persistent connection and uses a background event listener
    to dispatch events to pending request handlers via asyncio.Queue.
    Auto-reconnects on connection loss.
    """

    def __init__(self, url: str = OPENCLAW_WS_URL, token: str = OPENCLAW_GATEWAY_TOKEN):
        self.url = url
        self.token = token
        self.ws = None
        self._connected = False
        self._write_lock = asyncio.Lock()  # Serializes ws.send() calls
        self._connect_lock = asyncio.Lock()  # Prevents concurrent connect attempts
        self._listener_task = None  # Background event listener
        self._pending_requests: dict[str, asyncio.Queue] = {}  # request_id -> event queue
        self._followup_task = None  # Current follow-up listener (only one at a time)
        self._followup_request_id = None  # Request ID of the current follow-up listener

    async def connect(self):
        """Establish WebSocket connection, complete handshake, and start event listener."""
        if self._connected and self.ws is not None:
            return

        async with self._connect_lock:
            if self._connected and self.ws is not None:
                return

            logger.info(f"Connecting to OpenClaw at {self.url}")
            self.ws = await connect(self.url)

            # Wait for connect.challenge
            challenge_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            challenge = json.loads(challenge_text)

            if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
                raise Exception(f"Expected connect.challenge, got: {challenge}")

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

            if self.token:
                connect_request["params"]["auth"] = {"token": self.token}

            await self.ws.send(json.dumps(connect_request))

            response_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
            response = json.loads(response_text)

            if response.get("type") == "res" and response.get("ok"):
                logger.info("Connected to OpenClaw Gateway")
                self._connected = True
                self._start_listener()
            else:
                error = response.get("error", {})
                error_msg = error.get("message", str(response))
                raise Exception(f"Connect failed: {error_msg}")

    def _start_listener(self):
        """Start the background event listener if not already running."""
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(self._event_listener())

    async def _event_listener(self):
        """Persistent background loop that reads all WS events and dispatches to pending requests."""
        while True:
            try:
                if not self._connected or self.ws is None:
                    await asyncio.sleep(0.5)
                    continue

                event_text = await self.ws.recv()
                event = json.loads(event_text)

                # Dispatch to all pending request queues
                for queue in list(self._pending_requests.values()):
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass

            except ConnectionClosed:
                logger.warning("Connection to OpenClaw lost, will reconnect...")
                self._connected = False
                error_event = {"type": "internal_error", "error": "connection_lost"}
                for queue in list(self._pending_requests.values()):
                    try:
                        queue.put_nowait(error_event)
                    except asyncio.QueueFull:
                        pass
                await self._reconnect()

            except Exception as e:
                if self._connected:
                    logger.error(f"Event listener error: {e}")
                await asyncio.sleep(1)

    async def _reconnect(self):
        """Reconnect to OpenClaw with exponential backoff."""
        delay = 1.0
        while not self._connected:
            try:
                logger.info(f"Attempting to reconnect in {delay}s...")
                await asyncio.sleep(delay)
                self.ws = None
                async with self._connect_lock:
                    logger.info(f"Connecting to OpenClaw at {self.url}")
                    self.ws = await connect(self.url)

                    challenge_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                    challenge = json.loads(challenge_text)
                    if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
                        raise Exception(f"Expected connect.challenge, got: {challenge}")

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
                    if self.token:
                        connect_request["params"]["auth"] = {"token": self.token}

                    await self.ws.send(json.dumps(connect_request))
                    response_text = await asyncio.wait_for(self.ws.recv(), timeout=10.0)
                    response = json.loads(response_text)

                    if response.get("type") == "res" and response.get("ok"):
                        logger.info("Reconnected to OpenClaw Gateway")
                        self._connected = True
                        return
                    else:
                        error = response.get("error", {})
                        raise Exception(f"Connect failed: {error.get('message', str(response))}")

            except Exception as e:
                logger.error(f"Reconnect failed: {e}")
                delay = min(delay * 2, 30.0)

    async def disconnect(self):
        """Close the WebSocket connection."""
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        self._listener_task = None

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        self._connected = False
        logger.info("Disconnected from OpenClaw")

    async def send_message_with_quick_timeout(
        self,
        message: str,
        quick_timeout: float = QUICK_RESPONSE_TIMEOUT,
        full_timeout: float = OPENCLAW_TIMEOUT,
        followup_callback: Optional[FollowupCallback] = None,
    ) -> tuple[str | None, asyncio.Task | None]:
        """
        Send a chat message with quick timeout support for long-running tasks.

        When a response is received (state=final without pending tools), it's returned
        immediately. A follow-up listener keeps the queue alive to catch additional
        final events from embedded/background agents. Those results are sent via
        followup_callback.

        Args:
            message: The message to send
            quick_timeout: Timeout before switching to background mode (seconds)
            full_timeout: Maximum total time to wait for response (seconds)
            followup_callback: Async function called with result string when
                follow-up final events arrive from embedded agents

        Returns:
            Tuple of (result, continuation_task):
            - If response received: (result_string, None)
            - If quick timeout (no final state yet): (None, Task that will return result_string)
        """
        await self.connect()

        # Cancel any previous follow-up listener — new request supersedes it
        self._cancel_followup()

        request_id = str(uuid.uuid4())
        idempotency_key = str(uuid.uuid4())
        queue = asyncio.Queue()
        self._pending_requests[request_id] = queue
        handed_off = False

        try:
            chat_request = {
                "type": "req",
                "id": request_id,
                "method": "chat.send",
                "params": {
                    "sessionKey": OPENCLAW_SESSION_KEY,
                    "message": message,
                    "idempotencyKey": idempotency_key,
                },
            }

            async with self._write_lock:
                await self.ws.send(json.dumps(chat_request))

            # Wait for acknowledgment
            ack = await asyncio.wait_for(queue.get(), timeout=10.0)

            if ack.get("type") == "internal_error":
                return f"Error: {ack.get('error', 'connection lost')}", None

            if ack.get("type") == "res" and not ack.get("ok"):
                error = ack.get("error", {})
                return f"Error: {error.get('message', str(ack))}", None

            # Listen for events until completion or quick timeout
            response_content = ""
            quick_deadline = asyncio.get_event_loop().time() + quick_timeout

            while True:
                now = asyncio.get_event_loop().time()
                remaining = quick_deadline - now

                if remaining <= 0:
                    logger.info("Quick timeout expired, switching to background mode")
                    handed_off = True
                    continuation = asyncio.create_task(
                        self._continue_waiting(request_id, queue, response_content, followup_callback)
                    )
                    return None, continuation

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    logger.info("Quick timeout expired, switching to background mode")
                    handed_off = True
                    continuation = asyncio.create_task(
                        self._continue_waiting(request_id, queue, response_content, followup_callback)
                    )
                    return None, continuation

                if event.get("type") == "internal_error":
                    return f"Error: {event.get('error', 'connection lost')}", None

                response_content = _extract_response_content(event, response_content)
                state, has_pending_tools = _check_terminal_state(event)

                if state == "final":
                    if has_pending_tools:
                        logger.info("Terminal state with pending tools, waiting for next turn")
                        continue
                    logger.info(f"Request complete, response length={len(response_content)}")
                    # Spawn follow-up listener to catch embedded agent results
                    if followup_callback:
                        handed_off = True
                        asyncio.create_task(self._followup_listener(request_id, queue, followup_callback))
                    return response_content or "No response received", None
                elif state == "aborted":
                    logger.info("Request aborted")
                    return response_content or "Request was cancelled", None
                elif state == "error":
                    error_msg = event.get("payload", {}).get("error", "Unknown error")
                    logger.error(f"Request error: {error_msg}")
                    return f"Error: {error_msg}", None

        except ConnectionClosed as e:
            logger.error(f"Connection closed: {e}")
            self._connected = False
            raise Exception(f"Connection closed: {e}")

        except WebSocketException as e:
            logger.error(f"WebSocket error: {e}")
            self._connected = False
            raise Exception(f"WebSocket error: {e}")

        finally:
            if not handed_off and request_id in self._pending_requests:
                del self._pending_requests[request_id]

    async def _continue_waiting(
        self,
        request_id: str,
        queue: asyncio.Queue,
        initial_content: str = "",
        followup_callback: Optional[FollowupCallback] = None,
    ) -> str:
        """
        Continue waiting for OpenClaw response in background.

        After getting the first final result, spawns a follow-up listener
        to catch embedded agent results.
        """
        response_content = initial_content
        per_event_timeout = 120.0

        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=per_event_timeout)

                if event.get("type") == "internal_error":
                    return f"Error: {event.get('error', 'connection lost')}"

                response_content = _extract_response_content(event, response_content)
                state, has_pending_tools = _check_terminal_state(event)

                if state == "final":
                    if has_pending_tools:
                        continue
                    logger.info(f"Background task complete, response length={len(response_content)}")
                    # Spawn follow-up listener for embedded agent results
                    if followup_callback:
                        asyncio.create_task(self._followup_listener(request_id, queue, followup_callback))
                    else:
                        if request_id in self._pending_requests:
                            del self._pending_requests[request_id]
                    return response_content or "No response received"
                elif state == "aborted":
                    return response_content or "Request was cancelled"
                elif state == "error":
                    error_msg = event.get("payload", {}).get("error", "Unknown error")
                    return f"Error: {error_msg}"

        except asyncio.TimeoutError:
            return "Error: Request timed out - no response from OpenClaw"

        except Exception as e:
            return f"Error: {str(e)}"

        finally:
            # Only clean up if we didn't hand off to follow-up listener
            # (follow-up listener takes ownership of the queue)
            pass

    def _cancel_followup(self):
        """Cancel the current follow-up listener if one exists."""
        if self._followup_task and not self._followup_task.done():
            self._followup_task.cancel()
            logger.info(f"Cancelled previous follow-up listener for request {self._followup_request_id[:8]}")
        # Clean up the old follow-up's queue
        if self._followup_request_id and self._followup_request_id in self._pending_requests:
            del self._pending_requests[self._followup_request_id]
        self._followup_task = None
        self._followup_request_id = None

    async def _followup_listener(
        self,
        request_id: str,
        queue: asyncio.Queue,
        callback: FollowupCallback,
    ):
        """
        Keep listening for follow-up final events from embedded/background agents.

        After the initial response is returned, OpenClaw may spawn embedded agents
        that later post results back to the same session. This listener catches
        those results and sends them via the callback.

        Only one follow-up listener is active at a time. Starting a new request
        cancels any previous follow-up listener.

        Stops after FOLLOWUP_IDLE_TIMEOUT seconds of no events.
        """
        self._followup_task = asyncio.current_task()
        self._followup_request_id = request_id
        logger.info(f"Follow-up listener started for request {request_id[:8]}")
        response_content = ""

        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=FOLLOWUP_IDLE_TIMEOUT)

                if event.get("type") == "internal_error":
                    logger.warning("Follow-up listener: connection lost")
                    return

                response_content = _extract_response_content(event, response_content)
                state, has_pending_tools = _check_terminal_state(event)

                if state == "final" and not has_pending_tools:
                    if response_content:
                        logger.info(f"Follow-up result received, length={len(response_content)}, sending callback")
                        try:
                            await callback(response_content)
                        except Exception as e:
                            logger.error(f"Follow-up callback failed: {e}")
                    # Reset for potential additional follow-ups
                    response_content = ""

        except asyncio.CancelledError:
            logger.info(f"Follow-up listener cancelled for request {request_id[:8]}")

        except asyncio.TimeoutError:
            logger.info(f"Follow-up listener idle timeout ({FOLLOWUP_IDLE_TIMEOUT}s), stopping")

        except Exception as e:
            logger.error(f"Follow-up listener error: {e}")

        finally:
            if request_id in self._pending_requests:
                del self._pending_requests[request_id]
            # Clear tracking if we're still the active follow-up
            if self._followup_request_id == request_id:
                self._followup_task = None
                self._followup_request_id = None
            logger.info(f"Follow-up listener stopped for request {request_id[:8]}")

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
