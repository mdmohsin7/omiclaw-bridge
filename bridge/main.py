"""
OpenClaw Bridge - FastAPI server that bridges Omi to local OpenClaw.

This service runs on the user's local machine and:
1. Receives tool invocation requests from the Omi OpenClaw App
2. Verifies the request token
3. Forwards the request to local OpenClaw via WebSocket
4. Returns the response

Usage:
    python -m bridge

    Or with uvicorn:
    uvicorn bridge.main:app --host 0.0.0.0 --port 8081
"""

import logging

from dotenv import load_dotenv

load_dotenv()  # Load .env file before importing config

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import BRIDGE_HOST, BRIDGE_PORT, OMI_SECRET_TOKEN
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


class ToolResponse(BaseModel):
    """Response model for tool invocation."""

    result: str


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
    return len(token) == len(OMI_SECRET_TOKEN) and all(
        a == b for a, b in zip(token, OMI_SECRET_TOKEN)
    )


@app.post("/tools/ask", response_model=ToolResponse)
async def ask_openclaw(
    data: ToolRequest,
    x_omi_token: str = Header(None, alias="X-Omi-Token"),
):
    """
    Main tool endpoint - receives requests from Omi OpenClaw App and forwards to OpenClaw.

    Args:
        data: ToolRequest with the user's request
        x_omi_token: Secret token for verification

    Returns:
        ToolResponse with the result from OpenClaw
    """
    logger.info(f"Received request for user {data.uid}: {data.request[:100]}...")

    # Verify token
    if not verify_token(x_omi_token):
        logger.warning(f"Invalid or missing token for user {data.uid}")
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    try:
        # Forward request to OpenClaw
        result = await openclaw_client.send_message(data.request)
        logger.info(f"OpenClaw response for user {data.uid}: {result[:100]}...")
        return ToolResponse(result=result)

    except Exception as e:
        logger.error(f"Error processing request for user {data.uid}: {e}")
        return ToolResponse(result=f"Error: {str(e)}")


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
