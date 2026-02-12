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
- `volume/config_example/app.py` - example Telegram API config
- `volume/config_example/tg_ids.py` - example channels config
- `volume/config/` - local runtime config (ignored by git)
- `volume/last_processed_ids.json` - runtime state (ignored by git)
- `volume/sessions/` - Telethon sessions (ignored by git)

## Configuration

Create local config files (ignored by git):

1. `volume/config/app.py`
2. `volume/config/tg_ids.py`

Use examples from `volume/config_example/`.

Expected fields:

- `app.py`: `api_id`, `api_hash`
- `tg_ids.py`: `post_channel_id`, `steal_channel_ids`

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

- `volume/config/` and `volume/sessions/` are ignored and should never be committed.
- container runs as non-root user;
- compose file uses read-only root filesystem and dropped Linux capabilities.
