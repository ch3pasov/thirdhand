# ТРОЙСЫРЬЁ (Thirdhand)

Telegram repost bot on `Telethon` with watermark processing:

- listens for new posts in source channels (`steal_channel_ids`);
- republishes to target channel (`post_channel_id`);
- keeps album order and format;
- applies watermark to photos and videos;
- supports catch-up after restart via stored last processed IDs.

## Features

- **Reliable restart behavior**: remembers last handled message ID per source channel.
- **Albums preserved**: photos/videos are posted in the same order as original.
- **Photo watermark**: channel avatar (from target channel) is applied with random position.
- **Video watermark**:
  - same avatar as watermark;
  - square watermark, semi-transparent;
  - DVD-like bouncing motion;
  - starts from bottom-left corner.
- **Debug mode**: detailed startup and ffmpeg progress logs.

## Requirements

- Python 3.11+
- `ffmpeg` (for video watermarking)
- Telegram API credentials (`api_id`, `api_hash`)

Or run with Docker (recommended for VPS).

## Project structure

- `main.py` - bot logic
- `volume/runtime_example/config.py` - config example with empty values
- `volume/runtime_example/state.json` - state file example
- `volume/runtime_example/sessions/.gitkeep` - sessions folder example
- `volume/runtime/config.py` - local runtime config (ignored by git)
- `volume/runtime/state.json` - local runtime state (ignored by git)
- `volume/runtime/sessions/` - local Telethon sessions (ignored by git)

## Configuration

Create local runtime files from examples:

```bash
mkdir -p volume/runtime/sessions
cp volume/runtime_example/config.py volume/runtime/config.py
cp volume/runtime_example/state.json volume/runtime/state.json
```

Edit one config file:

- `volume/runtime/config.py`

Expected fields in that file:

- `api_id`, `api_hash`
- `post_channel_id`
- `steal_channel_ids`
- optional `debug = True/False` (default startup debug mode)

## Local run

Install dependencies:

```bash
pip install -r requirements.txt
```

Run:

```bash
python main.py
```

Debug mode:

```bash
python main.py --debug
```

## Docker / VPS run

Build and start:

```bash
docker compose build
docker compose up -d
```

View logs:

```bash
docker compose logs -f thirdhand
```

Debug run in container:

```bash
docker compose run --rm thirdhand python main.py --debug
```

## Security notes

- keep secrets in `volume/runtime/config.py` private on your server;
- container runs as non-root user;
- compose file uses read-only root filesystem and dropped Linux capabilities.
