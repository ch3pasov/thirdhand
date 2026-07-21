import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

import main as thirdhand


class LogFormattingTests(unittest.TestCase):
    def test_post_log_omits_message_body(self):
        secret_body = "private source post body"
        message = SimpleNamespace(
            id=42,
            date=datetime(2026, 1, 2, tzinfo=timezone.utc),
            text=secret_body,
            media=SimpleNamespace(),
        )
        event = SimpleNamespace(chat_id=-100123, message=message)

        formatted = thirdhand.format_post(event)

        self.assertNotIn(secret_body, formatted)
        self.assertIn(f"text_length={len(secret_body)}", formatted)
        self.assertIn("message_id=42", formatted)

    def test_album_log_omits_message_body(self):
        secret_body = "album caption that must not be logged"
        first = SimpleNamespace(
            id=8,
            grouped_id=777,
            date=datetime(2026, 1, 2, tzinfo=timezone.utc),
            text=secret_body,
        )
        second = SimpleNamespace(id=9)
        event = SimpleNamespace(chat_id=-100123, messages=[first, second])

        formatted = thirdhand.format_album(event)

        self.assertNotIn(secret_body, formatted)
        self.assertIn(f"text_length={len(secret_body)}", formatted)
        self.assertIn("message_ids=[8, 9]", formatted)


class StateTests(unittest.TestCase):
    def test_state_round_trip_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch.object(thirdhand, "STATE_PATH", state_path):
                thirdhand.save_state({"-1001": 12})
                self.assertEqual(thirdhand.load_state(), {"-1001": 12})

                state_path.write_text(
                    '{"-1001": "13", "bad": "not-an-id", "none": null}\n',
                    encoding="utf-8",
                )
                self.assertEqual(thirdhand.load_state(), {"-1001": 13})

    def test_missing_state_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "nested" / "state.json"
            with patch.object(thirdhand, "STATE_PATH", state_path):
                self.assertEqual(thirdhand.load_state(), {})
                self.assertEqual(state_path.read_text(encoding="utf-8"), "{}\n")


class MediaHelpersTests(unittest.TestCase):
    def test_video_detection(self):
        self.assertTrue(
            thirdhand.is_video_message(SimpleNamespace(video=object(), document=None))
        )
        self.assertTrue(
            thirdhand.is_video_message(
                SimpleNamespace(
                    video=None,
                    document=SimpleNamespace(mime_type="video/mp4"),
                )
            )
        )
        self.assertFalse(
            thirdhand.is_video_message(
                SimpleNamespace(
                    video=None,
                    document=SimpleNamespace(mime_type="image/jpeg"),
                )
            )
        )

    def test_ffmpeg_progress_parsing(self):
        self.assertEqual(
            thirdhand.parse_ffmpeg_progress_seconds("out_time", "01:02:03.5"),
            3723.5,
        )
        self.assertEqual(
            thirdhand.parse_ffmpeg_progress_seconds("out_time_us", "2500000"),
            2.5,
        )
        self.assertIsNone(
            thirdhand.parse_ffmpeg_progress_seconds("out_time", "broken")
        )
        self.assertIsNone(
            thirdhand.parse_ffmpeg_progress_seconds("frame", "2500000")
        )

    def test_ffmpeg_speed_parsing(self):
        self.assertEqual(thirdhand.parse_ffmpeg_speed_factor("1.25x"), 1.25)
        self.assertIsNone(thirdhand.parse_ffmpeg_speed_factor("N/A"))
        self.assertIsNone(thirdhand.parse_ffmpeg_speed_factor("0x"))
        self.assertIsNone(thirdhand.parse_ffmpeg_speed_factor("broken"))

    def test_photo_watermark_changes_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "input.jpg"
            watermark_path = root / "watermark.png"
            output_path = root / "output.jpg"

            source = np.zeros((240, 320, 3), dtype=np.uint8)
            watermark = np.zeros((64, 64, 4), dtype=np.uint8)
            watermark[:, :, 2] = 255
            watermark[:, :, 3] = 255
            self.assertTrue(cv2.imwrite(str(input_path), source))
            self.assertTrue(cv2.imwrite(str(watermark_path), watermark))

            with patch.object(thirdhand.random, "randint", side_effect=lambda low, high: low):
                thirdhand.apply_watermark_to_photo(
                    input_path, output_path, watermark_path
                )

            result = cv2.imread(str(output_path), cv2.IMREAD_COLOR)
            self.assertIsNotNone(result)
            self.assertEqual(result.shape, source.shape)
            self.assertGreater(int(result[:, :, 2].sum()), 0)


class ReactionHelpersTests(unittest.TestCase):
    def test_paid_reaction_random_id_combines_time_and_entropy(self):
        with patch.object(thirdhand.time, "time", return_value=123), patch.object(
            thirdhand.random, "getrandbits", return_value=456
        ):
            self.assertEqual(
                thirdhand.make_paid_reaction_random_id(), (123 << 32) | 456
            )

    def test_reaction_error_markers(self):
        self.assertTrue(
            thirdhand._is_insufficient_stars_error(Exception("BALANCE_TOO_LOW"))
        )
        self.assertTrue(
            thirdhand._is_random_id_expired_error(Exception("random_id_expired"))
        )
        self.assertFalse(thirdhand._is_insufficient_stars_error(Exception("other")))


if __name__ == "__main__":
    unittest.main()
