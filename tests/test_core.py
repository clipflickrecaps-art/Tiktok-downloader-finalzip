import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CoreTests(unittest.TestCase):
    def test_tiktok_url_validation(self):
        import downloader
        self.assertTrue(downloader.is_valid_tiktok_url("https://www.tiktok.com/@user/video/123"))
        self.assertTrue(downloader.is_valid_tiktok_url("https://vm.tiktok.com/abc/"))
        self.assertFalse(downloader.is_valid_tiktok_url("https://example.com/?next=https://tiktok.com/x"))

    def test_feature_thresholds(self):
        import settings
        self.assertTrue(settings.can_enable("cooldown_enabled", 0))
        self.assertFalse(settings.can_enable("premium_enabled", 999))
        self.assertTrue(settings.can_enable("premium_enabled", 1000))

    def test_database_initialises_in_isolated_directory(self):
        import database
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(database, "DB_FILE", str(Path(tmp) / "bot.db")):
                database.init_db()
                database.add_user(123, "tester", "Test")
                self.assertIsNotNone(database.get_user(123))
                self.assertEqual(database.get_total_users(), 1)

    def test_stale_file_cleanup(self):
        import downloader
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(downloader, "TEMP_DIR", tmp):
                old = Path(tmp) / "old.mp4"
                old.write_bytes(b"x")
                os.utime(old, (0, 0))
                self.assertEqual(downloader.cleanup_stale_files(1), 1)
                self.assertFalse(old.exists())

    def test_miniapp_download_argument_validation(self):
        import miniapp
        self.assertEqual(
            miniapp._parse_download_args({"platform": "youtube", "type": "video", "height": "720"}),
            ("youtube", "video", 720),
        )
        self.assertIsNone(miniapp._parse_download_args({"platform": "youtube", "type": "video", "height": "bad"}))
        self.assertIsNone(miniapp._parse_download_args({"platform": "unknown", "type": "video", "height": 0}))

    def test_miniapp_job_limit(self):
        import miniapp
        miniapp._active_jobs.clear()
        self.assertTrue(miniapp._start_job(77))
        self.assertFalse(miniapp._start_job(77))
        miniapp._finish_job(77)
        self.assertTrue(miniapp._start_job(77))
        miniapp._finish_job(77)

    def test_atomic_youtube_quota(self):
        import database
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(database, "DB_FILE", str(Path(tmp) / "bot.db")):
                database.init_db()
                self.assertTrue(database.consume_yt_daily(5, 1))
                self.assertFalse(database.consume_yt_daily(5, 1))

    def test_miniapp_origin_and_token_policy(self):
        import miniapp
        with patch.dict(os.environ, {"MINIAPP_ORIGIN": "https://app.example.com"}):
            self.assertEqual(miniapp._cors_origin(), "https://app.example.com")
        self.assertTrue(miniapp._TOKEN_RE.fullmatch("a" * 24 + ".mp4"))
        self.assertFalse(miniapp._TOKEN_RE.fullmatch("../secret.mp4"))


if __name__ == "__main__":
    unittest.main()
