"""
OpenClaw Bridge - FastAPI server that bridges Omi to local OpenClaw.

This service runs on the user's local machine and:
1. Receives tool invocation requests from the Omi OpenClaw App
2. Verifies the request token
3. Forwards the request to local OpenClaw via WebSocket
4. Returns the response (or acknowledgment for long-running tasks)

Long-running task handling:
- If OpenClaw responds within the quick timeout, returns result directly
- If timeout passes with no response, returns acknowledgment and
  continues in background, then calls the callback_url with the result

Embedded agent handling:
- After returning the initial response, a follow-up listener stays active
- If OpenClaw's embedded agents produce additional results (posted back
  to the same session), they are sent via callback_url

Usage:
    python -m bridge

    Or with uvicorn:
    uvicorn bridge.main:app --host 0.0.0.0 --port 8081
"""

import asyncio
import logging
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()  # Load .env file before importing config

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import BRIDGE_HOST, BRIDGE_PORT, OMI_SECRET_TOKEN, QUICK_RESPONSE_TIMEOUT
from .openclaw_client import openclaw_client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="OpenClaw Bridge",
    description="Bridge service connecting Omi to local OpenClaw AI assistant",
    version="1.0.0",
)

# Add CORS middleware for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ToolRequest(BaseModel):
    """Request model for tool invocation."""

    request: str
    uid: str
    callback_url: Optional[str] = None


class ToolResponse(BaseModel):
    """Response model for tool invocation."""

    result: str
    is_background: bool = False


def verify_token(token: str) -> bool:
    """
    Verify the token from the Omi OpenClaw App.

    Args:
        token: The X-Omi-Token header value

    Returns:
        True if token is valid, False otherwise
    """
    if not OMI_SECRET_TOKEN:
        # No token configured - skip verification (development mode)
        logger.warning("OMI_SECRET_TOKEN not set - skipping token verification (NOT SECURE)")
        return True

    if not token:
        return False

    # Constant-time comparison to prevent timing attacks
    return len(token) == len(OMI_SECRET_TOKEN) and all(a == b for a, b in zip(token, OMI_SECRET_TOKEN))


async def send_callback(callback_url: str, uid: str, result: str, x_omi_token: str):
    """
    Send the result to the callback URL when a background task completes.

    Args:
        callback_url: The URL to POST the result to
        uid: The user ID
        result: The result from OpenClaw
        x_omi_token: Token to include in callback for verification
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                callback_url,
                json={"uid": uid, "result": result},
                headers={"X-Omi-Token": x_omi_token or ""},
            )
            logger.info(f"Callback sent, status: {response.status_code}")
    except Exception as e:
        logger.error(f"Failed to send callback: {e}")


async def background_task_handler(continuation_task: asyncio.Task, callback_url: str, uid: str, x_omi_token: str):
    """
    Handle a background task - wait for it to complete and send callback.

    Args:
        continuation_task: The task waiting for OpenClaw response
        callback_url: URL to call with the result
        uid: User ID
        x_omi_token: Token for callback verification
    """
    try:
        result = await continuation_task
        logger.info(f"Background task completed for user {uid}")
        await send_callback(callback_url, uid, result, x_omi_token)
    except Exception as e:
        logger.error(f"Background task failed for user {uid}: {e}")
        await send_callback(callback_url, uid, f"Error: {str(e)}", x_omi_token)


@app.post("/tools/ask", response_model=ToolResponse)
async def ask_openclaw(
    data: ToolRequest,
    x_omi_token: str = Header(None, alias="X-Omi-Token"),
):
    """
    Main tool endpoint - receives requests from Omi OpenClaw App and forwards to OpenClaw.

    Long-running task handling:
    - If OpenClaw responds within QUICK_RESPONSE_TIMEOUT, returns result directly
    - If timeout with no response, returns acknowledgment with is_background=True
    - Background task continues and calls callback_url when complete

    Embedded agent handling:
    - After returning the initial response, a follow-up listener catches
      additional results from OpenClaw's embedded agents
    - Those results are sent via callback_url
    """
    # Verify token
    if not verify_token(x_omi_token):
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    # Create follow-up callback for embedded agent results
    followup_callback = None
    if data.callback_url:

        async def followup_callback(result: str):
            logger.info(f"Sending follow-up result for user {data.uid}")
            await send_callback(data.callback_url, data.uid, result, x_omi_token)

    try:
        # Forward request to OpenClaw with quick timeout support
        result, continuation_task = await openclaw_client.send_message_with_quick_timeout(
            data.request,
            followup_callback=followup_callback,
        )

        if result is not None:
            # Got response within quick timeout
            return ToolResponse(result=result, is_background=False)
        else:
            # Quick timeout expired, task is running in background
            if continuation_task and data.callback_url:
                # Spawn background handler to wait for result and send callback
                asyncio.create_task(
                    background_task_handler(continuation_task, data.callback_url, data.uid, x_omi_token)
                )
                return ToolResponse(
                    result="OpenClaw is working on this task and will send the result directly when complete. No further action needed.",
                    is_background=True,
                )
            elif continuation_task:
                # No callback URL provided, wait for the full result (blocks)
                result = await continuation_task
                return ToolResponse(result=result, is_background=False)
            else:
                return ToolResponse(result="Error: No response from OpenClaw", is_background=False)

    except Exception as e:
        return ToolResponse(result=f"Error: {str(e)}", is_background=False)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    openclaw_ok = await openclaw_client.health_check()
    token_configured = bool(OMI_SECRET_TOKEN)
    return {
        "status": "ok",
        "service": "openclaw-bridge",
        "openclaw_connected": openclaw_ok,
        "token_configured": token_configured,
    }


@app.get("/")
async def root():
    """Root endpoint with service info."""
    token_configured = bool(OMI_SECRET_TOKEN)
    return {
        "service": "OpenClaw Bridge",
        "version": "1.0.0",
        "description": "Bridge service connecting Omi to local OpenClaw AI assistant",
        "token_configured": token_configured,
        "endpoints": {
            "/tools/ask": "POST - Forward tool requests to OpenClaw",
            "/health": "GET - Health check",
        },
    }


@app.on_event("startup")
async def startup_event():
    """Connect to OpenClaw on startup for persistent connection."""
    try:
        await openclaw_client.connect()
    except Exception as e:
        logger.warning(f"Could not connect to OpenClaw on startup: {e} (will retry on first request)")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    await openclaw_client.disconnect()


def main():
    """Run the bridge server."""
    import uvicorn

    logger.info(f"Starting OpenClaw Bridge on {BRIDGE_HOST}:{BRIDGE_PORT}")
    logger.info("Make sure OpenClaw is running at ws://127.0.0.1:18789")

    if not OMI_SECRET_TOKEN:
        logger.warning("=" * 60)
        logger.warning("WARNING: OMI_SECRET_TOKEN is not set!")
        logger.warning("Set it to the token shown when you configured the Omi app.")
        logger.warning("Without it, anyone can send requests to this bridge.")
        logger.warning("=" * 60)
    else:
        logger.info("Token verification enabled")

    logger.info("Expose this server via ngrok: ngrok http 8081")

    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT)


if __name__ == "__main__":
    main()
