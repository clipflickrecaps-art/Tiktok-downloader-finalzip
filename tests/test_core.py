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

    def test_tiktok_original_url_selection_preserves_default_fallback(self):
        import downloader
        hd = "https://cdn.example/hd.mp4"
        normal = "https://cdn.example/play.mp4"
        self.assertEqual(
            downloader.get_original_video_url({"hdplay": hd, "play": normal}), hd
        )
        self.assertEqual(
            downloader.get_original_video_url({"hd_play": hd, "play": normal}), hd
        )
        self.assertIsNone(downloader.get_original_video_url({"play": normal}))
        self.assertEqual(downloader.get_best_video_url({"play": normal}), normal)

    def test_feature_thresholds(self):
        import settings
        self.assertTrue(settings.can_enable("cooldown_enabled", 0))
        self.assertTrue(settings.can_enable("premium_enabled", 0))
        self.assertEqual(settings.FLAG_DEFAULTS["premium_enabled"], "1")

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

    def test_manual_payment_approval_activates_premium(self):
        import database
        import settings
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(database, "DB_FILE", str(Path(tmp) / "bot.db")):
                database.init_db()
                settings.init_settings()
                plan_id = settings.add_premium_plan("Test Pro", 7, 5000, "MMK")
                account_id = settings.add_payment_account("ManualPay", "Owner", "09-123")
                order = settings.create_payment_order(77, plan_id, account_id)
                self.assertEqual(order["status"], "awaiting_proof")
                self.assertTrue(settings.submit_payment_proof(order["id"], 77, "proof-1", "photo", "TX-1"))
                self.assertEqual(len(settings.list_pending_payment_orders()), 1)
                result = settings.review_payment_order(order["id"], True, 999)
                self.assertEqual(result["status"], "approved")
                with database._connect() as conn:
                    row = conn.execute("SELECT plan_name FROM premium WHERE user_id = 77").fetchone()
                self.assertEqual(row["plan_name"], "Test Pro")

    def test_pro_queue_requires_entitlement_and_persists_status(self):
        import database
        import pro
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(database, "DB_FILE", str(Path(tmp) / "bot.db")):
                database.init_db()
                with self.assertRaises(PermissionError):
                    pro.create_batch(77, ["https://www.tiktok.com/@a/video/1"])
                self.assertTrue(pro.grant_pro(77, 30, 999))
                batch = pro.create_batch(77, [
                    "https://www.tiktok.com/@a/video/1",
                    "https://youtu.be/example",
                ])
                self.assertEqual(batch["total"], 2)
                job = pro.claim_next_job()
                self.assertEqual(job["status"], "pending")
                self.assertTrue(pro.finish_job(job["id"], "failed", "temporary"))
                self.assertTrue(pro.retry_job(77, job["id"]))

    def test_deleted_plan_is_deactivated_and_paid_premium_can_bulk_queue(self):
        import database
        import settings
        import pro
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(database, "DB_FILE", str(Path(tmp) / "bot.db")):
                database.init_db()
                plan_id = settings.add_premium_plan("Monthly Pro", 30, 5000, "MMK")
                self.assertTrue(settings.delete_premium_plan(plan_id))
                with database._connect() as conn:
                    row = conn.execute("SELECT is_active FROM premium_plans WHERE id = ?", (plan_id,)).fetchone()
                    self.assertEqual(row["is_active"], 0)
                    deleted = conn.execute("SELECT deleted_at FROM premium_plans WHERE id = ?", (plan_id,)).fetchone()
                    self.assertIsNotNone(deleted["deleted_at"])
                self.assertIsNone(settings.toggle_premium_plan(plan_id))
                self.assertIsNone(settings.create_payment_order(55, plan_id))
                self.assertTrue(settings.grant_premium_admin(55, 30, "Monthly Pro", 999, plan_id=plan_id))
                self.assertTrue(pro.is_pro(55))
                batch = pro.create_batch(55, ["https://www.tiktok.com/@a/video/2"])
                self.assertEqual(batch["total"], 1)

    def test_plan_features_can_be_edited(self):
        import database
        import settings
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(database, "DB_FILE", str(Path(tmp) / "bot.db")):
                database.init_db()
                plan_id = settings.add_premium_plan("Custom", 30, 1000, "MMK")
                self.assertFalse(settings.get_plan_features(plan_id)["bulk_download"])
                self.assertTrue(settings.update_plan_feature(plan_id, "bulk_download", True))
                self.assertTrue(settings.update_plan_feature(plan_id, "bulk_limit", 7))
                self.assertTrue(settings.get_plan_features(plan_id)["bulk_download"])
                self.assertEqual(settings.get_plan_features(plan_id)["bulk_limit"], 7)

    def test_miniapp_origin_and_token_policy(self):
        import miniapp
        with patch.dict(os.environ, {"MINIAPP_ORIGIN": "https://app.example.com"}):
            self.assertEqual(miniapp._cors_origin(), "https://app.example.com")
        self.assertTrue(miniapp._TOKEN_RE.fullmatch("a" * 24 + ".mp4"))
        self.assertFalse(miniapp._TOKEN_RE.fullmatch("../secret.mp4"))


if __name__ == "__main__":
    unittest.main()
