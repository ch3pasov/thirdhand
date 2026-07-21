import asyncio
import argparse
import importlib.util
import json
import os
import random
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

import cv2
from telethon import TelegramClient, events, functions, types
from telethon import utils
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import PeerChannel

RUNTIME_DIR = Path("volume/runtime")
EXAMPLE_DIR = Path("volume/runtime_example")
CONFIG_PATH = RUNTIME_DIR / "config.py"
STATE_PATH = RUNTIME_DIR / "state.json"
SESSIONS_DIR = RUNTIME_DIR / "sessions"
SESSION_PATH = str(SESSIONS_DIR / "monitor_account")
MAX_RETRIES = 5
MEDIA_PREP_CONCURRENCY = 4
DEBUG_MODE = False

last_processed_ids: dict[str, int] = {}
state_lock = asyncio.Lock()
media_prep_semaphore = asyncio.Semaphore(MEDIA_PREP_CONCURRENCY)
reaction_mode_by_chat: dict[int, str] = {}
reaction_peer_cache: dict[int, object] = {}
fatal_handler_error: Exception | None = None
paid_reactions_available = True


def ensure_runtime_layout() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        example_config_path = EXAMPLE_DIR / "config.py"
        if not example_config_path.exists():
            raise FileNotFoundError(
                "Missing runtime config template: volume/runtime_example/config.py"
            )
        shutil.copyfile(example_config_path, CONFIG_PATH)
        print(
            "Created runtime config from example: volume/runtime/config.py",
            flush=True,
        )

    if not STATE_PATH.exists():
        example_state_path = EXAMPLE_DIR / "state.json"
        if example_state_path.exists():
            shutil.copyfile(example_state_path, STATE_PATH)
        else:
            STATE_PATH.write_text("{}\n", encoding="utf-8")


def load_runtime_config():
    ensure_runtime_layout()
    spec = importlib.util.spec_from_file_location(
        "thirdhand_runtime_config", CONFIG_PATH
    )
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load runtime config module from volume/runtime/config.py")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_config = load_runtime_config()
api_id = int(os.environ.get("TELEGRAM_API_ID") or runtime_config.api_id)
api_hash = os.environ.get("TELEGRAM_API_HASH") or runtime_config.api_hash
post_channel_id = runtime_config.post_channel_id
steal_channel_ids = runtime_config.steal_channel_ids
config_debug = getattr(runtime_config, "debug", False)


def format_post(event: events.NewMessage.Event) -> str:
    message = event.message
    lines = [
        "",
        "=" * 60,
        f"[{datetime.now().isoformat(timespec='seconds')}] New post",
        f"chat_id={event.chat_id} message_id={message.id}",
        f"date={message.date.isoformat() if message.date else '-'}",
        f"text={message.text or '<no text>'}",
    ]

    if message.media:
        lines.append(f"media={type(message.media).__name__}")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_album(event: events.Album.Event) -> str:
    messages = event.messages
    first = messages[0]
    lines = [
        "",
        "=" * 60,
        f"[{datetime.now().isoformat(timespec='seconds')}] New album",
        f"chat_id={event.chat_id} grouped_id={first.grouped_id}",
        f"message_ids={[m.id for m in messages]}",
        f"date={first.date.isoformat() if first.date else '-'}",
        f"text={first.text or '<no text>'}",
        f"items={len(messages)}",
        "=" * 60,
    ]
    return "\n".join(lines)


def load_state() -> dict[str, int]:
    if not STATE_PATH.exists():
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text("{}\n", encoding="utf-8")
        return {}

    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    result: dict[str, int] = {}
    for key, value in payload.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def save_state(state: dict[str, int]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(STATE_PATH)


async def get_last_processed_id(chat_id: int) -> int:
    async with state_lock:
        return int(last_processed_ids.get(str(chat_id), 0))


async def mark_processed(chat_id: int, message_id: int) -> None:
    key = str(chat_id)
    async with state_lock:
        current = int(last_processed_ids.get(key, 0))
        if message_id <= current:
            return
        last_processed_ids[key] = message_id
        save_state(last_processed_ids)


def apply_watermark_to_photo(
    input_path: Path, output_path: Path, watermark_path: Path
) -> None:
    image = cv2.imread(str(input_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot read image for watermark: {input_path}")

    watermark = cv2.imread(str(watermark_path), cv2.IMREAD_UNCHANGED)
    if watermark is None:
        raise ValueError(f"Cannot read channel avatar watermark: {watermark_path}")

    if len(watermark.shape) == 2:
        watermark = cv2.cvtColor(watermark, cv2.COLOR_GRAY2BGRA)
    elif watermark.shape[2] == 3:
        alpha = 255 * (watermark[:, :, 0] > -1).astype("uint8")
        watermark = cv2.merge((watermark[:, :, 0], watermark[:, :, 1], watermark[:, :, 2], alpha))

    height, width = image.shape[:2]
    # Watermark side is 10% of source image width.
    side = max(44, int(width * 0.09))
    watermark = cv2.resize(watermark, (side, side), interpolation=cv2.INTER_AREA)

    margin = max(16, int(min(width, height) * 0.03))
    max_x = max(0, width - side - margin)
    max_y = max(0, height - side - margin)
    min_x = min(margin, max_x)
    min_y = min(margin, max_y)
    x = random.randint(min_x, max_x) if max_x > min_x else min_x
    y = random.randint(min_y, max_y) if max_y > min_y else min_y

    wm_rgb = watermark[:, :, :3].astype("float32")
    wm_alpha = (watermark[:, :, 3].astype("float32") / 255.0) * 0.24
    wm_alpha = wm_alpha[:, :, None]

    roi = image[y:y + side, x:x + side].astype("float32")
    blended = roi * (1.0 - wm_alpha) + wm_rgb * wm_alpha
    image[y:y + side, x:x + side] = blended.astype("uint8")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), image):
        raise ValueError(f"Cannot write watermarked image: {output_path}")


def is_video_message(message) -> bool:
    if getattr(message, "video", None) is not None:
        return True
    document = getattr(message, "document", None)
    if not document:
        return False
    mime_type = getattr(document, "mime_type", "") or ""
    return mime_type.startswith("video/")


def get_video_duration_seconds(video_path: Path) -> float | None:
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        return None
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        duration = float((result.stdout or "").strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def parse_ffmpeg_progress_seconds(key: str, value: str) -> float | None:
    if key == "out_time":
        try:
            hh, mm, ss = value.split(":")
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
        except ValueError:
            return None
    if key in {"out_time_us", "out_time_ms"}:
        try:
            raw = int(value)
        except ValueError:
            return None
        # ffmpeg progress keys out_time_us/out_time_ms are commonly microseconds.
        return raw / 1_000_000
    return None


def parse_ffmpeg_speed_factor(value: str) -> float | None:
    cleaned = value.strip().lower()
    if cleaned.endswith("x"):
        cleaned = cleaned[:-1]
    if cleaned in {"", "n/a"}:
        return None
    try:
        speed = float(cleaned)
    except ValueError:
        return None
    if speed <= 0:
        return None
    return speed


def debug_log(message: str) -> None:
    if DEBUG_MODE:
        print(f"[debug] {message}", flush=True)


def _is_insufficient_stars_error(error: Exception) -> bool:
    marker = str(error).upper()
    known_markers = (
        "BALANCE_TOO_LOW",
        "NOT_ENOUGH_STARS",
        "STARS_TOO_LOW",
        "STARS_BALANCE",
        "NO_STARS",
    )
    return any(token in marker for token in known_markers)


def _is_random_id_expired_error(error: Exception) -> bool:
    return "RANDOM_ID_EXPIRED" in str(error).upper()


def make_paid_reaction_random_id() -> int:
    # Telegram expects time-based random_id for paid reactions in raw API.
    unix_seconds = int(time.time())
    entropy = random.getrandbits(32)
    return (unix_seconds << 32) | entropy


async def get_own_stars_balance(client: TelegramClient) -> int | None:
    if not hasattr(functions, "payments"):
        return None
    if not hasattr(functions.payments, "GetStarsStatusRequest"):
        return None

    try:
        status = await client(
            functions.payments.GetStarsStatusRequest(
                peer="me",
            )
        )
    except Exception as error:
        debug_log(f"startup: failed to fetch stars balance: {error}")
        return None

    balance = getattr(status, "balance", None)
    if balance is None:
        return None

    amount = int(getattr(balance, "amount", 0) or 0)
    # One paid reaction costs one whole star.
    return amount


async def try_send_preferred_reaction(
    client: TelegramClient, source_peer, source_chat_id: int, source_msg_id: int
) -> None:
    global paid_reactions_available

    if source_msg_id <= 0:
        return

    resolved_source_peer = source_peer or reaction_peer_cache.get(source_chat_id)
    if resolved_source_peer is None:
        resolved_source_peer = await resolve_entity_safe(client, source_chat_id)
    reaction_peer_cache[source_chat_id] = resolved_source_peer

    known_mode = reaction_mode_by_chat.get(source_chat_id)
    can_try_paid = paid_reactions_available and hasattr(
        functions.messages, "SendPaidReactionRequest"
    )

    if known_mode != "heart" and known_mode != "none" and can_try_paid:
        try:
            # Explicit random_id avoids sporadic RANDOM_ID_EXPIRED errors.
            await client(
                functions.messages.SendPaidReactionRequest(
                    peer=resolved_source_peer,
                    msg_id=source_msg_id,
                    count=1,
                    random_id=make_paid_reaction_random_id(),
                    private=types.PaidReactionPrivacyDefault(),
                )
            )
            reaction_mode_by_chat[source_chat_id] = "paid"
            print(
                f"Applied paid star reaction to original post: chat_id={source_chat_id} "
                f"message_id={source_msg_id}",
                flush=True,
            )
            return
        except RPCError as error:
            if _is_random_id_expired_error(error):
                try:
                    debug_log(
                        "paid star reaction got RANDOM_ID_EXPIRED; retrying once "
                        f"(chat_id={source_chat_id}, msg_id={source_msg_id})"
                    )
                    await client(
                        functions.messages.SendPaidReactionRequest(
                            peer=resolved_source_peer,
                            msg_id=source_msg_id,
                            count=1,
                            random_id=make_paid_reaction_random_id(),
                            private=types.PaidReactionPrivacyDefault(),
                        )
                    )
                    reaction_mode_by_chat[source_chat_id] = "paid"
                    print(
                        "Applied paid star reaction to original post after retry: "
                        f"chat_id={source_chat_id} message_id={source_msg_id}",
                        flush=True,
                    )
                    return
                except RPCError as retry_error:
                    error = retry_error
            if _is_insufficient_stars_error(error):
                paid_reactions_available = False
                print(
                    "Paid star reactions disabled: insufficient stars balance",
                    flush=True,
                )
            debug_log(
                "paid star reaction failed; trying heart fallback "
                f"(chat_id={source_chat_id}, msg_id={source_msg_id}, error={error})"
            )
            reaction_mode_by_chat[source_chat_id] = "heart"
        except Exception as error:
            debug_log(
                "paid star reaction failed unexpectedly; trying heart fallback "
                f"(chat_id={source_chat_id}, msg_id={source_msg_id}, error={error})"
            )
            reaction_mode_by_chat[source_chat_id] = "heart"

    if known_mode == "none":
        debug_log(
            "skip reaction: channel previously marked unsupported "
            f"(chat_id={source_chat_id})"
        )
        return

    try:
        await client(
            functions.messages.SendReactionRequest(
                peer=resolved_source_peer,
                msg_id=source_msg_id,
                reaction=[types.ReactionEmoji(emoticon="❤")],
                add_to_recent=False,
                big=False,
            )
        )
        reaction_mode_by_chat[source_chat_id] = "heart"
        print(
            f"Applied heart reaction to original post: chat_id={source_chat_id} "
            f"message_id={source_msg_id}",
            flush=True,
        )
    except RPCError as error:
        reaction_mode_by_chat[source_chat_id] = "none"
        reaction_peer_cache.pop(source_chat_id, None)
        debug_log(
            "heart reaction failed; skipping reactions for this channel "
            f"(chat_id={source_chat_id}, msg_id={source_msg_id}, error={error})"
        )
    except Exception as error:
        reaction_mode_by_chat[source_chat_id] = "none"
        reaction_peer_cache.pop(source_chat_id, None)
        debug_log(
            "unexpected heart reaction failure; skipping this channel reactions "
            f"(chat_id={source_chat_id}, msg_id={source_msg_id}, error={error})"
        )


async def fail_fast(client: TelegramClient, error: Exception) -> None:
    global fatal_handler_error
    if fatal_handler_error is None:
        fatal_handler_error = error
    print(f"Fatal handler error: {error}", flush=True)
    await client.disconnect()


def apply_watermark_to_video(
    input_path: Path, output_path: Path, watermark_path: Path
) -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise ValueError("ffmpeg is required for video watermarking but was not found")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # DVD-like bounce. At t=0: x=0 (left), y=H-h (bottom).
    # scale2ref ties watermark size to the main video width and keeps it square.
    filter_complex = (
        "[1:v][0:v]scale2ref=w=trunc(main_w*0.09):h=trunc(main_w*0.09)[wm][base];"
        "[wm]format=rgba,colorchannelmixer=aa=0.24[wm2];"
        "[base][wm2]overlay="
        "x=abs(mod(t*240\\,2*(W-w))-(W-w)):"
        "y=H-h-abs(mod(t*190\\,2*(H-h))-(H-h)):"
        "eval=frame:shortest=1[v]"
    )
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(input_path),
        "-loop",
        "1",
        "-i",
        str(watermark_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output_path),
    ]
    if DEBUG_MODE:
        expected_duration = get_video_duration_seconds(input_path)
        debug_cmd = cmd[:-1] + [
            "-progress",
            "pipe:2",
            "-nostats",
            cmd[-1],
        ]
        print(
            f"[debug] video watermark start: {input_path.name} -> {output_path.name}",
            flush=True,
        )
        process = subprocess.Popen(
            debug_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        last_reported_second = -1
        started_at = time.monotonic()
        last_status_print_at = 0.0
        latest_progress_seconds = 0.0
        latest_speed = None
        ffmpeg_stderr_tail: list[str] = []

        def print_status(force: bool = False) -> None:
            nonlocal last_status_print_at
            now = time.monotonic()
            if not force and now - last_status_print_at < 2.0:
                return
            elapsed = now - started_at

            encoded_seconds = latest_progress_seconds
            estimated = False
            if encoded_seconds <= 0 and latest_speed and expected_duration:
                encoded_seconds = min(elapsed * latest_speed, expected_duration)
                estimated = True

            if expected_duration:
                percent = min((encoded_seconds / expected_duration) * 100, 100)
                if latest_speed:
                    remaining = max(expected_duration - encoded_seconds, 0.0)
                    eta = remaining / latest_speed
                    eta_part = f" eta={eta:.1f}s"
                else:
                    eta_part = ""
                approx = "~" if estimated else ""
                speed_part = f" speed={latest_speed:.2f}x" if latest_speed else ""
                print(
                    f"[debug] video watermark progress: {input_path.name} "
                    f"encoded={approx}{encoded_seconds:.1f}s/{expected_duration:.1f}s "
                    f"({percent:.1f}%) wall={elapsed:.1f}s{speed_part}{eta_part}",
                    flush=True,
                )
            else:
                speed_part = f" speed={latest_speed:.2f}x" if latest_speed else ""
                print(
                    f"[debug] video watermark progress: {input_path.name} "
                    f"encoded={encoded_seconds:.1f}s wall={elapsed:.1f}s{speed_part}",
                    flush=True,
                )
            last_status_print_at = now

        assert process.stderr is not None
        for raw_line in process.stderr:
            line = raw_line.strip()
            if "=" not in line:
                if line:
                    ffmpeg_stderr_tail.append(line)
                    if len(ffmpeg_stderr_tail) > 20:
                        ffmpeg_stderr_tail = ffmpeg_stderr_tail[-20:]
                continue
            key, value = line.split("=", 1)
            progress_seconds = parse_ffmpeg_progress_seconds(key, value)
            if progress_seconds is not None:
                latest_progress_seconds = max(latest_progress_seconds, progress_seconds)
                second = int(progress_seconds)
                if second > last_reported_second:
                    print_status(force=True)
                    last_reported_second = second
            elif key == "speed":
                latest_speed = parse_ffmpeg_speed_factor(value)
                print_status(force=False)
            elif key == "progress" and value == "end":
                print_status(force=True)
                print(
                    f"[debug] video watermark done: {output_path.name}",
                    flush=True,
                )
        return_code = process.wait()
        if return_code != 0:
            tail = "\n".join(ffmpeg_stderr_tail[-8:])
            raise ValueError(
                "ffmpeg failed to watermark video "
                f"(exit={return_code}). Last stderr lines:\n{tail}"
            )
    else:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(
                "ffmpeg failed to watermark video: "
                + (result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "")
            )


async def download_channel_watermark(client: TelegramClient, target_entity, workdir: Path) -> Path:
    watermark_path_str = await client.download_profile_photo(
        target_entity,
        file=str(workdir / "post_channel_avatar"),
        download_big=True,
    )
    if not watermark_path_str:
        raise ValueError(
            "Target post channel has no avatar. Set an avatar for post_channel_id "
            "or disable watermarking logic."
        )
    return Path(watermark_path_str)


async def prepare_repost_file(
    client: TelegramClient, message, workdir: Path, watermark_path: Path
) -> Path:
    async with media_prep_semaphore:
        original_path_str = await client.download_media(message, file=str(workdir))
        if not original_path_str:
            raise ValueError(f"Cannot download media for message_id={message.id}")

        original_path = Path(original_path_str)
        if message.photo:
            suffix = original_path.suffix or ".jpg"
            watermarked_path = workdir / f"{original_path.stem}_wm{suffix}"
            await asyncio.to_thread(
                apply_watermark_to_photo, original_path, watermarked_path, watermark_path
            )
            return watermarked_path
        if is_video_message(message):
            suffix = original_path.suffix or ".mp4"
            watermarked_path = workdir / f"{original_path.stem}_wm{suffix}"
            await asyncio.to_thread(
                apply_watermark_to_video, original_path, watermarked_path, watermark_path
            )
            return watermarked_path

        return original_path


async def safe_send(send_operation_name: str, send_coro_factory) -> None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await send_coro_factory()
            return
        except FloodWaitError as error:
            wait_seconds = error.seconds + 1
            print(f"FloodWait: sleeping {wait_seconds}s", flush=True)
            await asyncio.sleep(wait_seconds)
        except Exception as error:
            if attempt == MAX_RETRIES:
                raise
            backoff = 2 ** (attempt - 1)
            print(
                f"{send_operation_name} failed ({attempt}/{MAX_RETRIES}): {error}. "
                f"Retrying in {backoff}s",
                flush=True,
            )
            await asyncio.sleep(backoff)


async def resolve_entity_safe(client: TelegramClient, source):
    try:
        return await client.get_entity(source)
    except ValueError:
        # Numeric channel IDs in config are often stored as -100..., convert to PeerChannel.
        if isinstance(source, int):
            source_str = str(source)
            if source_str.startswith("-100"):
                channel_id = int(source_str[4:])
                return await client.get_entity(PeerChannel(channel_id))
        raise


async def forward_single_message(
    client: TelegramClient,
    target_entity,
    source_entity,
    message,
    watermark_path: Path,
    apply_source_reaction: bool = True,
) -> None:
    if message.id is None or message.chat_id is None:
        return

    chat_id = int(message.chat_id)
    last_id = await get_last_processed_id(chat_id)
    if message.id <= last_id:
        return

    async def _send_single() -> None:
        with tempfile.TemporaryDirectory(prefix="thirdhand-single-") as tmpdir:
            if message.media:
                repost_file = await prepare_repost_file(
                    client, message, Path(tmpdir) / "item_000", watermark_path
                )
                await client.send_file(
                    entity=target_entity,
                    file=str(repost_file),
                    caption=message.text or None,
                    supports_streaming=True,
                )
            else:
                await client.send_message(
                    entity=target_entity,
                    message=message.text or "",
                )

    await safe_send("Single repost", _send_single)
    if apply_source_reaction:
        await try_send_preferred_reaction(client, source_entity, chat_id, message.id)
    else:
        debug_log(
            "skip source reaction for catch-up single post "
            f"(chat_id={chat_id}, msg_id={message.id})"
        )
    await mark_processed(chat_id, message.id)


async def forward_album_messages(
    client: TelegramClient,
    target_entity,
    source_entity,
    messages: Sequence,
    watermark_path: Path,
    apply_source_reaction: bool = True,
) -> None:
    if not messages:
        return

    first = messages[0]
    if first.chat_id is None:
        return

    chat_id = int(first.chat_id)
    album_ids = [m.id for m in messages if m.id is not None]
    if not album_ids:
        return

    last_id = await get_last_processed_id(chat_id)
    max_album_id = max(album_ids)
    if max_album_id <= last_id:
        return

    async def _send_album() -> None:
        with tempfile.TemporaryDirectory(prefix="thirdhand-album-") as tmpdir:
            workdir = Path(tmpdir)
            prep_tasks = [
                prepare_repost_file(
                    client,
                    message,
                    workdir / f"item_{index:03d}",
                    watermark_path,
                )
                for index, message in enumerate(messages)
            ]
            prepared_files = await asyncio.gather(*prep_tasks)
            files = [str(path) for path in prepared_files]
            captions = [message.text or None for message in messages]

            await client.send_file(
                entity=target_entity,
                file=files,
                caption=captions,
                supports_streaming=True,
            )

    await safe_send("Album repost", _send_album)
    # Treat album as one post: react to the first message in the group.
    if apply_source_reaction:
        await try_send_preferred_reaction(
            client,
            source_entity,
            chat_id,
            min(album_ids),
        )
    else:
        debug_log(
            "skip source reaction for catch-up album "
            f"(chat_id={chat_id}, msg_id={min(album_ids)})"
        )
    await mark_processed(chat_id, max_album_id)


async def process_missed_posts(client: TelegramClient, target_entity, watermark_path: Path) -> None:
    for source in steal_channel_ids:
        try:
            entity = await resolve_entity_safe(client, source)
        except ValueError as error:
            raise ValueError(
                "Cannot resolve source channel from steal_channel_ids: "
                f"{source}. Ensure this account has access and the channel "
                "identifier is valid."
            ) from error
        source_chat_id = utils.get_peer_id(entity)
        last_id = await get_last_processed_id(source_chat_id)
        if last_id == 0:
            # First run: set watermark to current top message to avoid full-history repost.
            latest_messages = [m async for m in client.iter_messages(entity, limit=1)]
            if latest_messages and latest_messages[0].id is not None:
                await mark_processed(source_chat_id, latest_messages[0].id)
            continue

        print(
            f"Catch-up check: source={source} chat_id={source_chat_id} last_id={last_id}",
            flush=True,
        )

        pending_messages = [
            message
            async for message in client.iter_messages(
                entity,
                min_id=last_id,
                reverse=True,
            )
        ]
        if not pending_messages:
            continue

        index = 0
        while index < len(pending_messages):
            message = pending_messages[index]
            grouped_id = message.grouped_id

            if grouped_id:
                album_messages = [message]
                index += 1
                while (
                    index < len(pending_messages)
                    and pending_messages[index].grouped_id == grouped_id
                ):
                    album_messages.append(pending_messages[index])
                    index += 1

                album_ids = [m.id for m in album_messages if m.id is not None]
                print(
                    "Catch-up repost: creating album post "
                    f"source_chat_id={source_chat_id} message_ids={album_ids}",
                    flush=True,
                )
                await forward_album_messages(
                    client,
                    target_entity,
                    entity,
                    album_messages,
                    watermark_path,
                    apply_source_reaction=False,
                )
            else:
                print(
                    "Catch-up repost: creating single post "
                    f"source_chat_id={source_chat_id} message_id={message.id}",
                    flush=True,
                )
                await forward_single_message(
                    client,
                    target_entity,
                    entity,
                    message,
                    watermark_path,
                    apply_source_reaction=False,
                )
                index += 1


async def main() -> None:
    global paid_reactions_available

    if not steal_channel_ids:
        raise ValueError(
            "steal_channel_ids is empty. Update volume/runtime/config.py first."
        )
    if not post_channel_id:
        raise ValueError(
            "post_channel_id is not set. Update volume/runtime/config.py first."
        )

    last_processed_ids.update(load_state())

    client = TelegramClient(SESSION_PATH, api_id, api_hash)
    target_entity = None
    watermark_path = None
    runtime_tmpdir = tempfile.TemporaryDirectory(prefix="thirdhand-runtime-")
    startup_started_at = time.monotonic()

    @client.on(events.Album(chats=steal_channel_ids))
    async def on_new_album(event: events.Album.Event) -> None:
        try:
            if target_entity is None or watermark_path is None:
                return
            print(format_album(event), flush=True)
            source_entity = await event.get_input_chat()
            await forward_album_messages(
                client,
                target_entity,
                source_entity,
                event.messages,
                watermark_path,
            )
        except Exception as error:
            await fail_fast(client, error)

    @client.on(events.NewMessage(chats=steal_channel_ids))
    async def on_new_post(event: events.NewMessage.Event) -> None:
        try:
            if target_entity is None or watermark_path is None:
                return
            if event.message.grouped_id:
                # Album messages are forwarded by the Album handler as a group.
                return
            print(format_post(event), flush=True)
            source_entity = await event.get_input_chat()
            await forward_single_message(
                client,
                target_entity,
                source_entity,
                event.message,
                watermark_path,
            )
        except Exception as error:
            await fail_fast(client, error)

    debug_log("startup: connecting Telegram client")
    step_started_at = time.monotonic()
    await client.start()
    debug_log(f"startup: client connected in {time.monotonic() - step_started_at:.2f}s")
    stars_balance = await get_own_stars_balance(client)
    if stars_balance is None:
        print(
            "Stars balance check: unavailable (paid reactions will be attempted on demand)",
            flush=True,
        )
    elif stars_balance >= 1:
        print(
            f"Stars balance check: OK ({stars_balance} available)",
            flush=True,
        )
    else:
        paid_reactions_available = False
        print(
            "Stars balance check: insufficient (0). Paid star reactions disabled; "
            "heart reaction fallback will be used.",
            flush=True,
        )
    try:
        debug_log("startup: resolving target channel entity")
        step_started_at = time.monotonic()
        target_entity = await resolve_entity_safe(client, post_channel_id)
        debug_log(
            f"startup: target entity resolved in {time.monotonic() - step_started_at:.2f}s"
        )
    except ValueError as error:
        raise ValueError(
            "Cannot resolve post_channel_id. Open target channel in this account "
            "or use @username/invite link in config."
        ) from error

    try:
        debug_log("startup: downloading target channel avatar watermark")
        step_started_at = time.monotonic()
        watermark_path = await download_channel_watermark(
            client, target_entity, Path(runtime_tmpdir.name)
        )
        debug_log(
            "startup: watermark avatar downloaded in "
            f"{time.monotonic() - step_started_at:.2f}s"
        )
        print(f"Watermark avatar loaded: {watermark_path.name}", flush=True)

        debug_log("startup: catch-up processing started")
        step_started_at = time.monotonic()
        await process_missed_posts(client, target_entity, watermark_path)
        debug_log(
            f"startup: catch-up finished in {time.monotonic() - step_started_at:.2f}s"
        )
        debug_log(
            f"startup: ready in {time.monotonic() - startup_started_at:.2f}s total"
        )
        print(
            (
                f"Listening for new posts in channels: {steal_channel_ids}. "
                f"Forward target: {post_channel_id}"
            ),
            flush=True,
        )
        await client.run_until_disconnected()
        if fatal_handler_error is not None:
            raise RuntimeError("Fatal error in update handler") from fatal_handler_error
    finally:
        runtime_tmpdir.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Thirdhand repost worker")
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable verbose debug output (especially for video watermarking).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    DEBUG_MODE = args.debug or config_debug
    if DEBUG_MODE:
        print("[debug] Debug mode enabled", flush=True)
    asyncio.run(main())
