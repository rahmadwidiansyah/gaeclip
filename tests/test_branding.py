"""Clip branding tests: config, layout safety, filter order, render.

No network, no real credentials. Real-ffmpeg test is gated on the binary.
"""

import sys
import unittest

sys.path.insert(0, "tests")


class BrandConfigTests(unittest.TestCase):
    def test_disabled_by_default_and_old_payloads_valid(self):
        from gaeclip.branding import validate_brand_config

        self.assertFalse(validate_brand_config(None)["enabled"])
        self.assertFalse(validate_brand_config({})["enabled"])
        # Legacy job payloads without any brand keys stay valid + disabled.
        cfg = validate_brand_config({"subtitle_style": "x"})
        self.assertFalse(cfg["enabled"])

    def test_all_templates_accept_valid_config(self):
        from gaeclip.branding import TEMPLATES, validate_brand_config

        for template in TEMPLATES:
            cfg = validate_brand_config({
                "enabled": True, "template": template,
                "channel_name": "Kreator", "position": "top_left",
                "timing": "last", "duration": 2, "blur": 20,
                "dim": 0.35, "animation": "fade",
                "display_mode": "avatar_name",
            })
            self.assertEqual(cfg["template"], template)

    def test_invalid_rejected_or_normalized(self):
        from gaeclip.branding import validate_brand_config

        for bad in ({"template": "nope"}, {"position": "nope"},
                    {"display_mode": "nope"}, {"timing": "nope"},
                    {"animation": "nope"}, {"duration": "x"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                validate_brand_config({"enabled": True, **bad})
        # Out-of-range numerics normalize instead of raising.
        cfg = validate_brand_config({"duration": 99, "blur": -5, "dim": 9})
        self.assertEqual(cfg["duration"], 10.0)
        self.assertEqual(cfg["blur"], 0.0)
        self.assertEqual(cfg["dim"], 0.8)
        long_name = "x" * 200
        cfg = validate_brand_config({"channel_name": long_name})
        self.assertLessEqual(len(cfg["channel_name"]), 60)

    def test_special_chars_escaped_in_filter(self):
        from gaeclip.branding import _escape_drawtext, _escape_path

        nasty = "A:B,C'D[E]\\ hacking %{n}"
        esc = _escape_drawtext(nasty)
        for ch in (":", ",", "'", "[", "]"):
            self.assertNotIn(f"{ch}", esc.replace("\\" + ch, ""))
        self.assertIn("\\:", esc)
        self.assertIn("\\,", esc)
        path = _escape_path("C:\\ava'tar.png")
        self.assertNotIn("'", path.replace("\\'", ""))
        self.assertNotIn(":", path.replace("\\:", ""))


class BrandFontTests(unittest.TestCase):
    def test_windows_fontfile_is_preferred_and_escaped(self):
        import os
        from unittest.mock import patch

        from gaeclip.branding import resolve_brand_font

        def fake_exists(path):
            return path.replace("\\", "/").endswith("Windows/Fonts/segoeui.ttf")

        with patch.dict(os.environ, {"WINDIR": r"C:\Windows"}):
            with patch("gaeclip.branding.os.path.exists", side_effect=fake_exists):
                selector = resolve_brand_font()

        self.assertTrue(selector.startswith("fontfile='"))
        self.assertIn("segoeui.ttf", selector)
        # Drive colon must be escaped for ffmpeg's filtergraph parser.
        self.assertIn(r"C\:/Windows/Fonts/segoeui.ttf", selector)

    def test_fallback_to_fontconfig_name(self):
        import os
        from unittest.mock import patch

        from gaeclip.branding import resolve_brand_font

        with patch.dict(os.environ, {"WINDIR": r"C:\Windows"}):
            with patch("gaeclip.branding.os.path.exists", return_value=False):
                self.assertEqual(resolve_brand_font(), "font='DejaVu Sans'")


class BrandLayoutTests(unittest.TestCase):
    def _plan(self, **over):
        from gaeclip.branding import resolve_brand_plan, validate_brand_config

        cfg = validate_brand_config({
            "enabled": True, "channel_name": "Kreator Hebat Sekali",
            **over,
        })
        return resolve_brand_plan(cfg, over.get("_T", 30.0), 1080, 1920, None)

    def test_disabled_renders_plain(self):
        from gaeclip.branding import resolve_brand_plan, validate_brand_config

        cfg = validate_brand_config({})
        self.assertIsNone(resolve_brand_plan(cfg, 30.0, 1080, 1920, None))

    def test_avatar_only_without_avatar_skips(self):
        from gaeclip.branding import resolve_brand_plan, validate_brand_config

        cfg = validate_brand_config({"enabled": True, "display_mode": "avatar_only"})
        self.assertIsNone(resolve_brand_plan(cfg, 30.0, 1080, 1920, None))

    def test_endcard_window_short_clip(self):
        # Clip shorter than duration: whole clip is branded, never extended.
        plan = self._plan(template="glass_end", duration=3.0, _T=1.2)
        assert plan is not None
        self.assertEqual(plan.window[0], 0.0)
        self.assertAlmostEqual(plan.window[1], 1.2)
        plan = self._plan(template="creator_outro", duration=2.0, _T=30.0)
        assert plan is not None
        self.assertAlmostEqual(plan.window, (28.0, 30.0))

    def test_corner_positions_in_bounds(self):
        from gaeclip.branding import resolve_brand_plan, validate_brand_config

        for position in ("top_left", "top_right", "bottom_left",
                         "bottom_right", "top_center", "center"):
            cfg = validate_brand_config({
                "enabled": True, "template": "minimal_corner",
                "channel_name": "C", "position": position})
            plan = resolve_brand_plan(cfg, 30.0, 1080, 1920, None)
            assert plan is not None
            self.assertTrue(plan.bg_blur is False)
            if plan.avatar_xy is not None:
                ax, ay = plan.avatar_xy
                self.assertGreaterEqual(ax, 0)
                self.assertGreaterEqual(ay, 0)
                self.assertLess(ax + plan.avatar_size, 1080)
                self.assertLess(ay + plan.avatar_size, 1920)
            self.assertGreaterEqual(plan.text_y, 0)

    def test_tag_pill_in_bounds(self):
        from gaeclip.branding import resolve_brand_plan, validate_brand_config

        cfg = validate_brand_config({
            "enabled": True, "template": "creator_tag",
            "channel_name": "Nama Channel Yang Cukup Panjang Sekali",
            "position": "top_center"})
        plan = resolve_brand_plan(cfg, 30.0, 1080, 1920, None)
        assert plan is not None and plan.box is not None
        x, y, w, h = plan.box
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + w, 1080)

    def test_additional_text_outro_only(self):
        from gaeclip.branding import resolve_brand_plan, validate_brand_config

        cfg = validate_brand_config({
            "enabled": True, "template": "creator_outro",
            "channel_name": "C", "additional_text": "Subscribe"})
        plan = resolve_brand_plan(cfg, 30.0, 1080, 1920, None)
        assert plan is not None
        self.assertEqual(plan.additional_text, "Subscribe")
        cfg2 = validate_brand_config({
            "enabled": True, "template": "glass_end",
            "channel_name": "C", "additional_text": "Subscribe"})
        plan2 = resolve_brand_plan(cfg2, 30.0, 1080, 1920, None)
        assert plan2 is not None
        self.assertIsNone(plan2.additional_text)


class BrandFilterTests(unittest.TestCase):
    def _vf(self, **over):
        from gaeclip.branding import resolve_brand_plan, validate_brand_config, wrap_vf

        params = {"enabled": True, "channel_name": "Kreator", **over}
        duration = float(params.pop("_T", 30.0))
        cfg = validate_brand_config(params)
        plan = resolve_brand_plan(cfg, duration, 1080, 1920, None)
        assert plan is not None
        return wrap_vf("scale=1080:1920,setsar=1:1,fps=30",
                       ",subtitles=filename='x.ass'", plan, "font='DejaVu Sans'",
                       duration)

    def test_brand_before_subtitles(self):
        vf = self._vf(template="minimal_corner", position="top_left")
        self.assertIn("drawtext=", vf)
        self.assertIn("subtitles=", vf)
        self.assertLess(vf.index("drawtext="), vf.index("subtitles="))

    def test_endcard_has_blur_bg(self):
        vf = self._vf(template="glass_end")
        for token in ("gblur=", "overlay=0:0", "trim=start=", "drawtext="):
            self.assertIn(token, vf)
        self.assertIn("between(t,", vf)

    def test_avatar_chain_present_when_avatar(self):
        import tempfile
        from pathlib import Path

        from gaeclip.branding import resolve_brand_plan, validate_brand_config, wrap_vf

        with tempfile.TemporaryDirectory() as tmp:
            avatar = Path(tmp) / "av.png"
            avatar.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
            cfg = validate_brand_config({"enabled": True, "channel_name": "C"})
            plan = resolve_brand_plan(cfg, 30.0, 1080, 1920, avatar)
            assert plan is not None and plan.avatar_path is not None
            vf = wrap_vf("scale=1080:1920,fps=30", "", plan,
                         "font='DejaVu Sans'", 30.0, "[1:v]")
            self.assertIn("[1:v]", vf)
            self.assertIn("overlay=", vf)
            # Without an avatar input label the same plan must fail loudly,
            # never silently drop the avatar.
            with self.assertRaises(ValueError):
                wrap_vf("scale=1080:1920,fps=30", "", plan,
                        "font='DejaVu Sans'", 30.0)

    def test_no_black_when_source_missing(self):
        # Endcard background is a trim of the segment itself — the chain
        # must never reference black filler sources (shadow/box text colors
        # are fine and intentionally excluded from this check).
        vf = self._vf(template="creator_outro")
        self.assertNotIn("color=c=black", vf)
        self.assertNotIn("colorchannelmixer", vf)
        self.assertNotIn("nullsrc", vf)

    def test_dynamic_chain_labels(self):
        from gaeclip.branding import extend_complex, resolve_brand_plan, validate_brand_config

        cfg = validate_brand_config({"enabled": True, "channel_name": "C"})
        plan = resolve_brand_plan(cfg, 30.0, 1080, 1920, None)
        assert plan is not None
        filters: list[str] = ["[0:v]trim=start=0:end=5,setpts=PTS-STARTPTS[v0]",
                              "[v0]concat=n=1:v=1:a=0[vconcatraw]",
                              "[vconcatraw]fps=30[vconcat]"]
        out = extend_complex(filters, "vconcat", "subtitles=filename='x.ass'",
                             plan, "font='DejaVu Sans'", 30.0)
        self.assertEqual(out, "vout")
        joined = ";".join(filters)
        self.assertIn("[vout]", joined)
        self.assertLess(joined.index("overlay="), joined.index("subtitles="))


class BrandAvatarTests(unittest.TestCase):
    def test_pick_avatar_prefers_uncropped(self):
        from gaeclip.channel_avatar import pick_avatar_url

        payload = {"thumbnails": [
            {"id": "avatar", "url": "http://x/a.jpg"},
            {"id": "avatar_uncropped", "url": "http://x/b.jpg"},
            {"id": "banner", "url": "http://x/c.jpg"},
        ]}
        self.assertEqual(pick_avatar_url(payload), "http://x/b.jpg")
        self.assertIsNone(pick_avatar_url({}))
        self.assertIsNone(pick_avatar_url({"thumbnails": [{"id": "banner", "url": "http://x/c.jpg"}]}))

    def test_asset_guard_rejects_traversal(self):
        import tempfile

        from gaeclip.channel_avatar import resolve_asset_path

        with tempfile.TemporaryDirectory() as tmp:
            good = __import__("pathlib").Path(tmp) / "manual-abc123.png"
            good.write_bytes(b"\x89PNG\r\n\x1a\nxxx")
            from pathlib import Path

            result = resolve_asset_path("manual-abc123.png", Path(tmp))
            # Normalize for cross-platform comparison (Windows short names
            # may differ from the temp dir name).
            self.assertEqual(result.resolve(), good.resolve())
            for evil in ("../x.png", "/etc/passwd", "..\\x.png", "", "a/b.png"):
                self.assertIsNone(resolve_asset_path(evil, Path(tmp)))

    def test_upload_validation(self):
        import tempfile
        from pathlib import Path

        from gaeclip.channel_avatar import save_uploaded_avatar
        from gaeclip.errors import ValidationError

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValidationError):
                save_uploaded_avatar(b"not an image", "x.txt", Path(tmp))
            with self.assertRaises(ValidationError):
                save_uploaded_avatar(b"x" * (2 * 1024 * 1024 + 1), "x.png", Path(tmp))
            asset = save_uploaded_avatar(b"\x89PNG\r\n\x1a\n" + b"0" * 64, "av.png", Path(tmp))
            self.assertTrue(asset.startswith("manual-"))


class BrandResolveUrlTests(unittest.TestCase):
    def _client(self):
        import sys

        sys.path.insert(0, "tests")
        from test_settings_api import make_client

        return make_client()

    def _env_root(self, tmp):
        import os
        from unittest.mock import patch

        return patch.dict(os.environ, {"STORAGE_ROOT": str(tmp)})

    def test_avatar_upload_accepts_multipart(self):
        import tempfile

        client, _ = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            with self._env_root(tmp):
                res = client.post(
                    "/api/v1/brand-assets/avatar/upload",
                    files={"file": ("avatar.png", b"\x89PNG\r\n\x1a\n", "image/png")},
                )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["preview_url"].startswith("/api/v1/brand-assets/"))

    def test_invalid_url_rejected(self):
        client, _ = self._client()
        for bad in ("", "not a url", "https://vimeo.com/123"):
            res = client.post("/api/v1/brand-assets/avatar/resolve-url", json={"url": bad})
            self.assertEqual(res.status_code, 422, bad)

    def test_cache_hit_no_network(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from gaeclip.videos import VideoStore

        client, fake = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            with self._env_root(tmp):
                store = VideoStore(fake)
                store.upsert("vid123", "https://www.youtube.com/watch?v=vid123", {
                    "title": "T", "channel": "Dapur Kita",
                    "channel_id": "UCabc", "channel_url": "https://www.youtube.com/channel/UCabc",
                })
                asset = Path(tmp) / "assets" / "avatars" / "ch-UCabc.jpg"
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_bytes(b"\xff\xd8\xff" + b"0" * 2048)
                with patch("gaeclip.channel_avatar.fetch_channel_avatar",
                           return_value=asset) as fetched, \
                     patch("gaeclip.downloader.video_metadata") as meta:
                    res = client.post("/api/v1/brand-assets/avatar/resolve-url",
                                      json={"url": "https://www.youtube.com/watch?v=vid123"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["channel_name"], "Dapur Kita")
        self.assertEqual(body["asset_id"], "ch-UCabc.jpg")
        self.assertTrue(body["preview_url"].endswith("ch-UCabc.jpg"))
        meta.assert_not_called()

    def test_cache_miss_fetches_and_stores(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from gaeclip.videos import VideoStore

        client, fake = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            with self._env_root(tmp):
                asset = Path(tmp) / "assets" / "avatars" / "ch-UCxyz.jpg"
                payload = {
                    "title": "Video Baru", "channel": "Kanal Baru",
                    "channel_id": "UCxyz",
                    "channel_url": "https://www.youtube.com/channel/UCxyz",
                }
                with patch("gaeclip.downloader.video_metadata", return_value=payload) as meta, \
                     patch("gaeclip.channel_avatar.fetch_channel_avatar",
                           return_value=asset):
                    res = client.post("/api/v1/brand-assets/avatar/resolve-url",
                                      json={"url": "https://youtu.be/newvid12345"})
                meta.assert_called_once()
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["channel_name"], "Kanal Baru")
        # VideoStore now has it: the pipeline and future resolves reuse it.
        stored = VideoStore(fake).get("newvid12345")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.channel_name, "Kanal Baru")

    def test_ytdlp_failure_is_422_not_500(self):
        from unittest.mock import patch

        from gaeclip.errors import ExternalCommandError

        client, _ = self._client()
        with patch("gaeclip.downloader.video_metadata",
                   side_effect=ExternalCommandError("Sign in to confirm you're not a bot")):
            res = client.post("/api/v1/brand-assets/avatar/resolve-url",
                              json={"url": "https://www.youtube.com/watch?v=private123"})
        self.assertEqual(res.status_code, 422)
        self.assertIn("metadata", res.json()["detail"].lower())

    def test_avatar_missing_still_returns_name(self):
        from unittest.mock import patch

        client, _ = self._client()
        payload = {"title": "T", "channel": "Tanpa Avatar", "channel_id": "UCnoav"}
        with patch("gaeclip.downloader.video_metadata", return_value=payload), \
             patch("gaeclip.channel_avatar.fetch_channel_avatar", return_value=None):
            res = client.post("/api/v1/brand-assets/avatar/resolve-url",
                              json={"url": "https://www.youtube.com/watch?v=noav1234567"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["channel_name"], "Tanpa Avatar")
        self.assertIsNone(body["asset_id"])
        self.assertIsNone(body["preview_url"])

    def test_stale_avatar_refreshes_or_falls_back(self):
        import os
        import tempfile
        import time
        from pathlib import Path
        from unittest.mock import patch

        from gaeclip.channel_avatar import fetch_channel_avatar

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            stale = directory / "ch-UCold.jpg"
            stale.write_bytes(b"\xff\xd8\xff" + b"0" * 2048)
            old = time.time() - 40 * 86400
            os.utime(stale, (old, old))
            # Fresh network works: returns (possibly new) file, old removed.
            fresh = directory / "ch-UCold.jpg"
            with patch("gaeclip.media.run_command") as rc, \
                 patch("gaeclip.channel_avatar._download_image", return_value=fresh):
                rc.return_value = type("R", (), {"stdout": '{"thumbnails": []}'})()
                out = fetch_channel_avatar("UCold", None, directory)
            self.assertEqual(out, fresh)
            # Network dead: stale file returned, never None.
            with patch("gaeclip.media.run_command", side_effect=RuntimeError("net down")):
                out = fetch_channel_avatar("UCold", None, directory)
            self.assertEqual(out, stale)
            # Fresh cache: no network touched at all.
            now = time.time()
            os.utime(stale, (now, now))
            with patch("gaeclip.media.run_command") as rc:
                out = fetch_channel_avatar("UCold", None, directory)
            rc.assert_not_called()
            self.assertEqual(out, stale)


class BrandPipelineTests(unittest.TestCase):
    """Regression: pipeline + branding=ON must execute the real brand block.

    A lazy-import typo here once failed every clip of real jobs
    (ImportError inside the per-clip try) while all unit tests stayed
    green — this test drives generate_from_url with branding enabled.
    """

    def test_pipeline_with_branding_completes(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from gaeclip import pipeline as pipe_mod
        from gaeclip.config import Settings

        from test_roadmap import _make_vtt

        seen_plans = []

        def fake_metadata(url, yt_dlp="yt-dlp"):
            return {"duration": 200, "title": "Video Asli", "channel": "Kanal Uji"}

        def fake_subtitles(url, source_dir, language="id", yt_dlp="yt-dlp"):
            Path(source_dir).mkdir(parents=True, exist_ok=True)
            subtitle_file = Path(source_dir) / "subtitles.id.vtt"
            _make_vtt(subtitle_file)
            return subtitle_file

        def fake_section(url, output_dir, start, end, name, yt_dlp="yt-dlp"):
            return Path(output_dir) / f"{name}.mp4"

        def fake_render(source, output, start=0, duration=0, subtitles=None,
                        branding=None, **kwargs):
            seen_plans.append(branding)
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_bytes(b"x" * 200_000)
            return Path(output)

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env()
            object.__setattr__(settings, "storage_root", Path(directory))
            with (
                patch.object(pipe_mod, "video_metadata", fake_metadata),
                patch.object(pipe_mod, "download_subtitles", fake_subtitles),
                patch.object(pipe_mod, "download_video_section", fake_section),
                # static is bound at pipeline import time (patch the pipeline
                # namespace); dynamic/trajectory are imported lazily inside
                # the clip loop (patch at the media source).
                patch.object(pipe_mod, "render_static_vertical", fake_render),
                patch("gaeclip.media.render_dynamic_vertical", fake_render),
                patch("gaeclip.media.render_trajectory_vertical", fake_render),
                patch("gaeclip.channel_avatar.fetch_channel_avatar", return_value=None),
            ):
                result = pipe_mod.generate_from_url(
                    "https://www.youtube.com/watch?v=test",
                    settings=settings, min_duration=30, max_duration=60,
                    max_clips=2,
                    branding={"enabled": True, "template": "minimal_corner",
                              "channel_name": "Kanal Uji", "position": "top_left"},
                )
        done = [c for c in result.clips]
        self.assertGreaterEqual(len(done), 1)
        # Every render received a resolved plan (not None): the lazy brand
        # imports + validation + geometry all executed.
        self.assertTrue(seen_plans)
        for plan in seen_plans:
            self.assertIsNotNone(plan)
            self.assertEqual(plan.template, "minimal_corner")
            self.assertEqual(plan.channel_name, "Kanal Uji")

    def test_pipeline_lazy_brand_imports_resolve(self):
        # Static guard: every name pipeline imports lazily must exist.
        import ast
        from pathlib import Path

        tree = ast.parse(Path("src/gaeclip/pipeline.py").read_text(encoding="utf-8"))
        import gaeclip.branding as branding_mod

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == ".branding":
                imported.update(a.asname or a.name for a in node.names)
        missing = sorted(name for name in imported if not hasattr(branding_mod, name))
        self.assertEqual(missing, [])


class BrandRenderTests(unittest.TestCase):
    def test_static_render_with_branding_valid_output(self):
        import shutil
        import tempfile
        from pathlib import Path

        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("ffmpeg not available")
        from gaeclip.branding import resolve_brand_plan, validate_brand_config
        from gaeclip.media import render_static_vertical, verify_clip

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            from gaeclip.media import run_command

            run_command(["ffmpeg", "-y", "-f", "lavfi", "-i",
                         "testsrc=size=640x360:rate=30:duration=6",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                         "-c:v", "libx264", "-pix_fmt", "yuv420p",
                         "-c:a", "aac", str(src)])
            for template in ("glass_end", "minimal_corner", "creator_tag", "creator_outro"):
                cfg = validate_brand_config({
                    "enabled": True, "template": template,
                    "channel_name": "Kreator: Hebat, Uji!",
                    "position": "top_left" if template == "minimal_corner" else "top_center",
                    "duration": 2.0, "animation": "fade",
                    "additional_text": "Subscribe",
                })
                plan = resolve_brand_plan(cfg, 6.0, 270, 480, None)
                assert plan is not None
                clip_out = Path(tmp) / f"out-{template}.mp4"
                render_static_vertical(src, clip_out, start=0, duration=6.0,
                                       width=270, height=480, branding=plan,
                                       preset="veryfast")
                info = verify_clip(clip_out, expected_duration=6.0)
                self.assertIn("codec_type", info)
            # Avatar path: circle prep + movie overlay end-to-end.
            from gaeclip.branding import prepare_avatar

            raw = Path(tmp) / "raw.png"
            run_command(["ffmpeg", "-y", "-f", "lavfi", "-i",
                         "color=size=400x400:rate=1:color=red:duration=1",
                         "-frames:v", "1", str(raw)])
            circle = prepare_avatar(
                raw, 128, Path(tmp) / "circle.png")
            self.assertIsNotNone(circle)
            cfg = validate_brand_config({
                "enabled": True, "template": "creator_tag",
                "channel_name": "Kreator", "position": "top_center",
                "animation": "fade_scale",
            })
            plan = resolve_brand_plan(cfg, 6.0, 270, 480, circle)
            assert plan is not None and plan.show_avatar
            clip_av = Path(tmp) / "out-avatar.mp4"
            render_static_vertical(src, clip_av, start=0, duration=6.0,
                                   width=270, height=480, branding=plan,
                                   preset="veryfast")
            verify_clip(clip_av, expected_duration=6.0)

    def test_disabled_renders_legacy(self):
        import shutil
        import tempfile
        from pathlib import Path

        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("ffmpeg not available")
        from gaeclip.media import render_static_vertical, verify_clip
        from gaeclip.media import run_command

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.mp4"
            out = Path(tmp) / "out.mp4"
            run_command(["ffmpeg", "-y", "-f", "lavfi", "-i",
                         "testsrc=size=640x360:rate=30:duration=6",
                         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                         "-c:v", "libx264", "-pix_fmt", "yuv420p",
                         "-c:a", "aac", str(src)])
            render_static_vertical(src, out, start=0, duration=6.0,
                                   width=270, height=480, branding=None,
                                   preset="veryfast")
            verify_clip(out, expected_duration=6.0)


if __name__ == "__main__":
    unittest.main()
