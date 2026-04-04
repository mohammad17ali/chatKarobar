# chatKarobar

WhatsApp agent built on top of the [Baileys](https://github.com/WhiskeySockets/Baileys) framework, with a pluggable AI response backend.

## Prerequisites

- **Node.js >= 18** and npm
- A smartphone with WhatsApp installed (for QR code pairing)

## Quick Start

```bash
# Install dependencies
npm install

# Copy and configure environment variables
# Edit .env to set your log level, OpenAI key, etc.

# Start the agent
npm start
```

On first run a QR code will appear in your terminal. Scan it with **WhatsApp > Linked Devices > Link a Device** on your phone.

Once connected, any text message sent to the linked account will receive an automatic reply.

## Project Structure

```
src/
  index.js            Entry point -- Baileys socket, connection lifecycle, QR auth
  messageHandler.js   Incoming message filter and reply dispatch
  responseEngine.js   Reply generator (static string now; OpenAI later)
  logger.js           Pino logger -- console + file (logs/app.log)
```

## Scripts

| Command | Description |
|---------|-------------|
| `npm start` | Run the agent |
| `npm run dev` | Run with `--watch` (auto-restart on file changes, Node >= 18.11) |

## Configuration (.env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOG_LEVEL` | `info` | Pino log level |
| `OPENAI_API_KEY` | — | For future AI backend |
| `BOT_NAME` | `chatKarobar` | Bot display name |

## License

ISC
