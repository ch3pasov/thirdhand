# ТРОЙСЫРЬЁ (Thirdhand)

A small Telethon worker that republishes selected Telegram channel posts into
another channel and marks photos and videos with the destination avatar. It
preserves albums, catches up after restarts and keeps its deployment state on
disk.

Use it only with channels and media you are allowed to access and republish.

## Features

- **Reliable restart behavior**: remembers the last handled message ID for each source channel.
- **Albums preserved**: photos/videos are posted in the same order as original.
- **Photo watermark**: channel avatar (from target channel) is applied with random position.
- **Video watermark**:
  - same avatar as watermark;
  - square watermark, semi-transparent;
  - DVD-like bouncing motion;
  - starts from bottom-left corner.
- **Debug mode**: detailed startup and ffmpeg progress logs.
- **Source acknowledgement**: attempts a one-Star paid reaction on each new
  source post and falls back to a heart when paid reactions are unavailable.

Catch-up posts intentionally do not receive reactions. A successful paid
reaction spends one Telegram Star, so check the account balance before leaving
the worker unattended.

## Requirements

- Python 3.11+
- `ffmpeg` (for video watermarking)
- Telegram API credentials (`api_id`, `api_hash`)
- a Telegram user account that can read every source and post to the target

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

Edit `volume/runtime/config.py` and set:

- `post_channel_id`;
- `steal_channel_ids`;
- optional `debug = True/False`.

Copy `.env.example` to `.env` and provide `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH`. The matching `api_id` and `api_hash` fields in
`config.py` are retained only as a backwards-compatible fallback and should
stay empty in new installations.

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

Build and start with exported credentials or a local `.env` file:

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

`compose-with-secrets` is the owner's deployment wrapper for injecting the
same two credentials from a scoped 1Password vault. Plain Docker Compose is
the portable interface.

## Runtime and privacy

- `volume/runtime/sessions/` contains the authorized Telethon session and must
  never be committed or shared;
- `volume/runtime/state.json` stores only the last processed message ID per
  source channel;
- downloaded media and generated watermarks live in temporary directories and
  are removed after each send;
- operational logs include channel and message identifiers, media types and
  text lengths, but not post bodies or downloaded media;
- Docker rotates JSON logs at five 10 MB files.

The container runs without root privileges, capabilities or a writable root
filesystem. Its only persistent writable mount is `volume/`; `/tmp` is an
in-memory filesystem.

## Tests

The test runner builds the production image and executes the `unittest` suite
inside it with an isolated in-memory runtime directory:

```bash
./scripts/run_tests.sh
```

## Security notes

- keep `.env`, `volume/runtime/config.py` and the Telethon session private;
- container runs as non-root user;
- compose file uses read-only root filesystem and dropped Linux capabilities.

## License

No public license has been selected yet. Until a license file is added, normal
copyright restrictions apply; publishing the source alone would not grant
permission to copy, modify or redistribute it.
