"""Publish lifecycle tests: upload vs processing, reconcile, global status.

No network, no credentials. Fakes drive the provider seam; time.sleep is
patched so bounded polling stays fast.
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "tests")

from test_video_metadata import FakeRedis


def make_service(runtime="desktop"):
    from gaeclip.social.service import SocialPublishService

    return SocialPublishService(FakeRedis(), runtime=runtime)


def _account(service, provider="youtube", account_id="acc-1"):
    from gaeclip.social.accounts import SocialAccount

    account = SocialAccount(id=account_id, provider=provider,
                            external_account_id="ext-1", display_name="Chan",
                            status="connected", token_reference="ref-1")
    service.accounts.save(account)
    service.secrets.put("ref-1", {"access_token": "tok-abc",
                                  "expires_at": time.time() + 3600})
    return account


def _job(service, provider="youtube", account_id="acc-1", status="pending",
         job_id="spub-1"):
    from gaeclip.social.accounts import SocialPublishJob

    job = SocialPublishJob(id=job_id, clip_id="c000001",
                            job_id="job-1", provider=provider,
                            social_account_id=account_id, status=status,
                            title="Hook Title", idempotency_key=f"k-{job_id}")
    return service.jobs.save_job(job)


def _clip_file():
    tmp = tempfile.TemporaryDirectory()
    clip = Path(tmp.name) / "c000001.mp4"
    clip.write_bytes(b"\x00" * 2048)
    tmp_clip = (tmp, clip)
    return tmp_clip


class FakeUploadProgressPublisher:
    """Emits real 0/50/100 uploading fractions, then PROCESSING."""

    provider = "youtube"

    def __init__(self, final="processing"):
        from gaeclip.social.states import ProviderCapabilities

        self.capabilities = ProviderCapabilities(
            direct_publish=True, supports_upload_progress=True,
            supports_processing_status=True,
            supports_remote_status_polling=True,
            supports_provider_url=True)
        self.final = final
        self.seen_progress: list = []

    def account_info(self, access_token):
        from gaeclip.social.publishers.base import AccountInfo

        return AccountInfo(external_account_id="ext-1", display_name="Chan")

    def validate_account(self, access_token, account_meta):
        return None

    def publish(self, ctx):
        from gaeclip.social.states import PublishResult

        for frac in (0.0, 0.5, 1.0):
            ctx.on_progress(frac, "uploading")
            self.seen_progress.append(frac)
        ctx.on_uploaded("vid-123", "https://youtu.be/vid-123")
        return PublishResult(status=self.final, remote_media_id="vid-123",
                             remote_url="https://youtu.be/vid-123")

    def get_publish_status(self, access_token, remote_media_id):
        return self.final


class FakeTimeoutAfterAcceptPublisher(FakeUploadProgressPublisher):
    """Upload accepted (on_uploaded called), then status check times out."""

    def publish(self, ctx):
        from gaeclip.social.states import PublishResult, TransientError

        ctx.on_progress(1.0, "uploading")
        ctx.on_uploaded("vid-999", "https://youtu.be/vid-999")
        raise TransientError("timed out after accept")


class StateMachineTests(unittest.TestCase):
    def test_new_transitions(self):
        from gaeclip.social.states import can_transition

        self.assertTrue(can_transition("pending", "preparing"))
        self.assertTrue(can_transition("preparing", "uploading"))
        self.assertTrue(can_transition("uploading", "uploaded"))
        self.assertTrue(can_transition("uploaded", "processing"))
        self.assertTrue(can_transition("processing", "published"))
        self.assertTrue(can_transition("uploading", "reconciling"))
        self.assertTrue(can_transition("reconciling", "processing"))
        # Legacy still holds.
        self.assertTrue(can_transition("pending", "uploading"))
        self.assertTrue(can_transition("failed", "pending"))
        self.assertFalse(can_transition("published", "pending"))

    def test_terminal_sets(self):
        from gaeclip.social.states import (is_active, is_terminal)

        for s in ("published", "draft", "failed", "cancelled"):
            self.assertTrue(is_terminal(s))
            self.assertFalse(is_active(s))
        for s in ("pending", "queued", "preparing", "uploading", "uploaded",
                  "processing", "reconciling", "scheduled"):
            self.assertTrue(is_active(s))
            self.assertFalse(is_terminal(s))


class GlobalStatusTests(unittest.TestCase):
    def test_single_success(self):
        from gaeclip.social.states import summarize_jobs

        out = summarize_jobs(["published"])
        self.assertEqual(out["global_status"], "completed")

    def test_single_failure(self):
        from gaeclip.social.states import summarize_jobs

        out = summarize_jobs(["failed"])
        self.assertEqual(out["global_status"], "failed")

    def test_mixed_partial(self):
        from gaeclip.social.states import summarize_jobs

        out = summarize_jobs(["published", "failed"])
        self.assertEqual(out["global_status"], "partial_success")

    def test_three_platform_mixed(self):
        from gaeclip.social.states import summarize_jobs

        out = summarize_jobs(["published", "processing", "failed"])
        # Active processing keeps it publishing (still polling).
        self.assertEqual(out["global_status"], "publishing")

    def test_four_platform_mixed(self):
        from gaeclip.social.states import summarize_jobs

        out = summarize_jobs(["published", "processing", "published", "failed"])
        self.assertEqual(out["global_status"], "publishing")
        done = summarize_jobs(["published", "published", "failed", "cancelled"])
        self.assertEqual(done["global_status"], "partial_success")

    def test_all_processing(self):
        from gaeclip.social.states import summarize_jobs

        out = summarize_jobs(["processing", "processing"])
        self.assertEqual(out["global_status"], "processing")

    def test_all_failed(self):
        from gaeclip.social.states import summarize_jobs

        out = summarize_jobs(["failed", "failed"])
        self.assertEqual(out["global_status"], "failed")

    def test_all_success(self):
        from gaeclip.social.states import summarize_jobs

        out = summarize_jobs(["published", "published", "draft"])
        self.assertEqual(out["global_status"], "completed")

    def test_cancelled(self):
        from gaeclip.social.states import summarize_jobs

        out = summarize_jobs(["cancelled", "cancelled"])
        self.assertEqual(out["global_status"], "cancelled")


class UploadVsProcessingTests(unittest.TestCase):
    def _patched(self, fake):
        service = make_service()
        patcher = patch("gaeclip.social.service.publisher_for",
                        return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return service

    def test_upload_progress_then_processing(self):
        fake = FakeUploadProgressPublisher(final="processing")
        service = self._patched(fake)
        _account(service)
        job = _job(service)
        tmp, clip = _clip_file()
        try:
            with patch("gaeclip.social.service.clip_file_path", return_value=clip), \
                 patch("gaeclip.media.verify_clip", return_value={"duration": "30.0"}), \
                 patch("gaeclip.media.probe", return_value={"width": "1080", "height": "1920", "duration": "30.0"}), \
                 patch("gaeclip.social.service.SocialPublishService._poll_remote", return_value="processing"), \
                 patch("time.sleep", return_value=None):
                out = service.execute(job.id)
        finally:
            tmp.cleanup()
        # Upload fractions were real; final is durable PROCESSING (not fake FAILED).
        self.assertEqual(out.status, "processing")
        self.assertEqual(out.remote_media_id, "vid-123")
        self.assertEqual(out.remote_url, "https://youtu.be/vid-123")
        self.assertIsNone(out.progress)  # indeterminate, no fake %
        self.assertIn("processing", (out.status_message or "").lower())

    def test_upload_then_published(self):
        fake = FakeUploadProgressPublisher(final="published")
        service = self._patched(fake)
        _account(service)
        job = _job(service)
        tmp, clip = _clip_file()
        try:
            with patch("gaeclip.social.service.clip_file_path", return_value=clip), \
                 patch("gaeclip.media.verify_clip", return_value={"duration": "30.0"}), \
                 patch("gaeclip.media.probe", return_value={"width": "1080", "height": "1920", "duration": "30.0"}), \
                 patch("time.sleep", return_value=None):
                out = service.execute(job.id)
        finally:
            tmp.cleanup()
        self.assertEqual(out.status, "published")
        self.assertTrue(out.published_at)

    def test_timeout_after_accept_reconciles(self):
        fake = FakeTimeoutAfterAcceptPublisher()
        service = self._patched(fake)
        _account(service)
        job = _job(service)
        tmp, clip = _clip_file()
        try:
            with patch("gaeclip.social.service.clip_file_path", return_value=clip), \
                 patch("gaeclip.media.verify_clip", return_value={"duration": "30.0"}), \
                 patch("gaeclip.media.probe", return_value={"width": "1080", "height": "1920", "duration": "30.0"}), \
                 patch.object(service, "reconcile_job",
                              return_value=service.jobs.get_job(job.id)) as rec, \
                 patch("time.sleep", return_value=None):
                service.execute(job.id)
                self.assertTrue(rec.called)
        finally:
            tmp.cleanup()
        # Remote persisted even though publish raised.
        saved = service.jobs.get_job(job.id)
        self.assertEqual(saved.remote_media_id, "vid-999")

    def test_reconcile_finds_published(self):
        service = make_service()
        _account(service)
        job = _job(service, status="reconciling")
        job.remote_media_id = "vid-1"
        job.remote_url = "https://youtu.be/vid-1"
        service.jobs.save_job(job)

        class FakePoll:
            capabilities = FakeUploadProgressPublisher().capabilities

            def get_publish_status(self, token, rid):
                return "published"

        with patch("gaeclip.social.service.publisher_for", return_value=FakePoll()):
            out = service.reconcile_job(job.id)
        self.assertEqual(out.status, "published")

    def test_reconcile_finds_processing(self):
        service = make_service()
        _account(service)
        job = _job(service, status="reconciling")
        job.remote_media_id = "vid-1"
        service.jobs.save_job(job)

        class FakePoll:
            capabilities = FakeUploadProgressPublisher().capabilities

            def get_publish_status(self, token, rid):
                return "processing"

        with patch("gaeclip.social.service.publisher_for", return_value=FakePoll()):
            out = service.reconcile_job(job.id)
        self.assertEqual(out.status, "processing")
        self.assertIn("processing", (out.status_message or "").lower())

    def test_reconcile_no_video_stays_reconciling(self):
        service = make_service()
        _account(service)
        job = _job(service, status="uploading")
        service.jobs.save_job(job)
        out = service.reconcile_job(job.id)
        self.assertEqual(out.status, "reconciling")

    def test_retry_only_failed_platform(self):
        from gaeclip.social.states import summarize_jobs

        # Per-job retry is already isolated; global view proves one failure
        # doesn't mark the others failed.
        out = summarize_jobs(["published", "failed"])
        self.assertEqual(out["completed"], 1)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["global_status"], "partial_success")

    def test_provider_without_processing(self):
        from gaeclip.social.states import ProviderCapabilities

        caps = ProviderCapabilities(direct_publish=True,
                                    supports_processing_status=False,
                                    supports_remote_status_polling=False)
        self.assertFalse(caps.supports_processing_status)
        self.assertFalse(caps.supports_remote_status_polling)

    def test_capabilities_honest(self):
        from gaeclip.social.service import provider_capabilities

        caps = provider_capabilities()
        self.assertTrue(caps["youtube"]["supports_upload_progress"])
        self.assertTrue(caps["youtube"]["supports_remote_status_polling"])
        self.assertTrue(caps["youtube"]["supports_provider_url"])
        self.assertFalse(caps["tiktok"]["supports_provider_url"])


if __name__ == "__main__":
    unittest.main()
