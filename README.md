# OpenClaw Bridge for Omi

Local bridge service that connects [Omi](https://omi.me) to your local [OpenClaw](https://openclaw.ai) AI assistant.

## What is this?

This bridge runs on your local machine and receives requests from the Omi OpenClaw App, forwarding them to your local OpenClaw instance.

```
Omi App → Omi Backend → OpenClaw App (hosted) → This Bridge (local) → OpenClaw (local)
                                                      ↑
                                              You run this!
```

## Prerequisites

1. **OpenClaw** installed and running on your machine
   - OpenClaw should be running its WebSocket gateway at `ws://127.0.0.1:18789`
   - See [OpenClaw documentation](https://openclaw.ai) for setup

2. **Python 3.9+** installed

3. **ngrok** (or similar tunnel service) for exposing local server
   - Install: `brew install ngrok` or download from [ngrok.com](https://ngrok.com)

## Quick Start

### 1. Install dependencies

```bash
cd plugins/openclaw-bridge
pip install -r requirements.txt
```

### 2. Start OpenClaw

Make sure OpenClaw is running and accessible at `ws://127.0.0.1:18789`.

### 3. Configure the Omi OpenClaw App

1. Open the Omi app
2. Go to **Apps** → Search for **"OpenClaw Assistant"**
3. Enable the app and click **Configure**
4. Enter your ngrok URL (see step 5)
5. **Save the secret token** that's displayed - you'll need it next!

### 4. Set the secret token

Create a `.env` file in the `openclaw-bridge` directory:

```bash
OMI_SECRET_TOKEN=oc_your_token_here
```

### 5. Start the bridge

```bash
python -m bridge
```

The bridge will start on `http://localhost:8081`.

### 6. Expose via ngrok

In a new terminal:

```bash
ngrok http 8081
```

Copy the HTTPS URL (e.g., `https://abc123.ngrok.io`) and enter it in the Omi app configuration.

### 7. Start chatting!

In Omi's chat, you can now say:

- "Hey OpenClaw, what files do I have about taxes?"
- "OpenClaw, am I free tomorrow at 5pm?"
- "Ask OpenClaw to check my recent emails"

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCLAW_WS_URL` | `ws://127.0.0.1:18789` | OpenClaw WebSocket URL |
| `BRIDGE_HOST` | `0.0.0.0` | Bridge server host |
| `BRIDGE_PORT` | `8081` | Bridge server port |
| `OMI_SECRET_TOKEN` | (empty) | **Required** - Token from Omi app setup |
| `OPENCLAW_TIMEOUT` | `120.0` | Request timeout in seconds |

## API Endpoints

### `POST /tools/ask`

Receives tool invocation requests from the Omi OpenClaw App.

**Request:**
```json
{
  "request": "find files about quarterly report",
  "uid": "user_firebase_uid"
}
```

**Headers:**
- `X-Omi-Token`: Secret token for verification

**Response:**
```json
{
  "result": "Found 3 files matching 'quarterly report': report_q1.pdf, report_q2.xlsx, report_q3.docx"
}
```

### `GET /health`

Health check endpoint.

**Response:**
```json
{
  "status": "ok",
  "service": "openclaw-bridge",
  "openclaw_connected": true,
  "token_configured": true
}
```

## Security

- **Per-user token**: Each user gets a unique token when they configure the app
- **Token verification**: Bridge rejects requests without valid `X-Omi-Token`
- **Local-first**: All data processing happens locally on your machine
- **Encrypted tunnel**: ngrok provides HTTPS encryption for data in transit

**Important**: Always set `OMI_SECRET_TOKEN` in production. Without it, anyone who discovers your ngrok URL can send requests to your bridge.

## Troubleshooting

### "Invalid or missing token"

Make sure:
1. You've set `OMI_SECRET_TOKEN` environment variable
2. The token matches the one shown when you configured the Omi app
3. If you regenerated the token in the app, update your env var

### "Could not connect to OpenClaw"

Make sure OpenClaw is running:
```bash
# Check if OpenClaw is listening
curl -i --include --no-buffer \
  --header "Connection: Upgrade" \
  --header "Upgrade: websocket" \
  --header "Sec-WebSocket-Key: test" \
  --header "Sec-WebSocket-Version: 13" \
  http://127.0.0.1:18789/
```

### "Request timed out"

OpenClaw may be taking too long to process. Try:
1. Simplify your request
2. Increase `OPENCLAW_TIMEOUT` environment variable
3. Check OpenClaw logs for errors

## Development

Run with auto-reload:

```bash
uvicorn bridge.main:app --host 0.0.0.0 --port 8081 --reload
```

## License

MIT License - see [LICENSE](LICENSE) for details.
