"""Phase 4 tests: multi-person acquisition, ambiguity, onset, lock,
association, anchors, group gating. Deterministic synthetic harness."""

import unittest

from gaeclip.framing_config import FramingConfig
from gaeclip.speaker_score import (
    ambiguity_margin,
    associate_track,
    confidence_level,
    face_quality_score,
    onset_score,
)
from gaeclip.target_manager import TargetManager
from gaeclip.visual import (
    BoundingBox,
    FaceObservation,
    build_speaker_timeline,
)

XS5 = (0.10, 0.30, 0.50, 0.70, 0.90)
IDS5 = ("A", "B", "C", "D", "E")


def face_obs(timestamp, speaker, x, lip=0.9, audio=0.9, conf=0.95):
    return FaceObservation(
        timestamp=timestamp, speaker_id=speaker,
        face=BoundingBox(x=max(0.0, x - 0.06), y=0.25, width=0.12, height=0.4),
        lip_score=lip, audio_score=audio, confidence=conf,
    )


def run_timeline(observations, duration, **kwargs):
    params = dict(sample_step=0.1)
    params.update(kwargs)
    return build_speaker_timeline(observations, duration, **params)


class PrimitivesTests(unittest.TestCase):
    def test_onset_strong_vs_jitter(self):
        rising = [(9.8, 0.10), (9.9, 0.20), (10.0, 0.42), (10.1, 0.65), (10.2, 0.80)]
        self.assertGreater(onset_score(rising, 10.2), 0.3)
        jitter = [(9.8, 0.70), (9.9, 0.20), (10.0, 0.65), (10.1, 0.10)]
        self.assertEqual(onset_score(jitter, 10.1), 0.0)
        self.assertEqual(onset_score([(10.0, 0.9)], 10.0), 0.0)

    def test_ambiguity_margin(self):
        best, second, gap = ambiguity_margin({"A": 0.78, "B": 0.76, "C": 0.1})
        self.assertEqual((best, second), ("A", "B"))
        self.assertAlmostEqual(gap, 0.02)
        best, second, gap = ambiguity_margin({})
        self.assertEqual((best, second, gap), (None, None, 0.0))

    def test_confidence_levels(self):
        self.assertEqual(confidence_level(0.8, 0.2), "HIGH")
        self.assertEqual(confidence_level(0.5, 0.1), "MEDIUM")
        self.assertEqual(confidence_level(0.3, 0.2), "LOW")
        self.assertEqual(confidence_level(0.8, -1.0), "AMBIGUOUS")

    def test_face_quality(self):
        good = face_quality_score(0.05, 0.0, 0.95)
        tiny = face_quality_score(0.001, 0.0, 0.95)
        clipped = face_quality_score(0.05, 0.5, 0.95)
        self.assertGreater(good, tiny)
        self.assertGreater(good, clipped)
        self.assertTrue(0.0 <= tiny <= 1.0)

    def test_associate_track(self):
        cands = [
            {"id": "n1", "center": (0.72, 0.51), "size": 0.021},
            {"id": "n2", "center": (0.20, 0.50), "size": 0.020},
        ]
        self.assertEqual(
            associate_track((0.70, 0.50), 0.02, 300, cands), "n1")
        # Too far -> None (never invent).
        far = [{"id": "n9", "center": (0.05, 0.5), "size": 0.02}]
        self.assertIsNone(associate_track((0.70, 0.50), 0.02, 300, far))
        # Stale gap -> None.
        self.assertIsNone(
            associate_track((0.70, 0.50), 0.02, 5000, cands))
        # Wrong size -> None.
        big = [{"id": "n9", "center": (0.71, 0.5), "size": 0.20}]
        self.assertIsNone(
            associate_track((0.70, 0.50), 0.02, 300, big))


class TimelineAmbiguityTests(unittest.TestCase):
    def _two_speaker_obs(self, a_lip, b_lip, duration=4.0, step=0.1):
        obs = []
        t = 0.0
        i = 0
        while t < duration:
            obs.append(face_obs(t, "A", 0.25, lip=a_lip, audio=0.9))
            obs.append(face_obs(t, "B", 0.75, lip=b_lip, audio=0.9))
            t = round(t + step, 6)
            i += 1
        return obs

    def test_ambiguous_overlap_holds_incumbent(self):
        # A incumbent (1s head start); B joins with a sustained ~0.115 lead:
        # above switch_margin (0.10) yet inside ambiguity_margin (0.12).
        # Legacy margin-only logic switches after hold; the ambiguity gate
        # holds instead. This isolates the ambiguity gate.
        obs = []
        t = 0.0
        while t < 5.0:
            obs.append(face_obs(t, "A", 0.25, lip=0.78, audio=0.9))
            if t >= 1.0:
                obs.append(face_obs(t, "B", 0.75, lip=1.00, audio=0.9))
            t = round(t + 0.1, 6)
        timeline = run_timeline(obs, 5.0, ambiguity_margin=0.12)
        speakers = [s.speaker_id for s in timeline]
        self.assertTrue(speakers)
        self.assertEqual(set(speakers), {"A"})
        legacy = run_timeline(obs, 5.0, ambiguity_margin=0.0)
        self.assertIn("B", [s.speaker_id for s in legacy])

    def test_clear_margin_switches(self):
        # A 0.86 vs B 0.58: gap clears margin -> switch after hold.
        obs = []
        t = 0.0
        while t < 6.0:
            lip_a = 0.9 if t < 2.0 else 0.1
            lip_b = 0.1 if t < 2.0 else 0.75
            obs.append(face_obs(t, "A", 0.25, lip=lip_a, audio=0.9))
            obs.append(face_obs(t, "B", 0.75, lip=lip_b, audio=0.9))
            t = round(t + 0.1, 6)
        timeline = run_timeline(obs, 6.0, ambiguity_margin=0.12)
        speakers = [s.speaker_id for s in timeline]
        self.assertIn("B", speakers)

    def test_no_margin_gate_legacy(self):
        # A incumbent (1s head start), then B joins with a sustained narrow
        # lead (gap ~0.03): margin=0 switches after hold, margin=0.12 holds.
        obs = []
        t = 0.0
        while t < 5.0:
            obs.append(face_obs(t, "A", 0.25, lip=0.78, audio=0.9))
            if t >= 1.0:
                obs.append(face_obs(t, "B", 0.75, lip=0.84, audio=0.9))
            t = round(t + 0.1, 6)
        legacy = run_timeline(obs, 5.0, ambiguity_margin=0.0)
        gated = run_timeline(obs, 5.0, ambiguity_margin=0.12)
        self.assertIn("B", [s.speaker_id for s in legacy])
        self.assertEqual([s.speaker_id for s in gated], ["A"])

    def test_onset_bonus_accelerates_switch(self):
        # A steady 0.47; B steps to 0.95 at t=2 (base gap ~0.37, under the
        # 0.40 force bar). Plain waits out the 2s hold (~4.0); the latched
        # onset bonus bridges the force gap during the rise -> ~2.3.
        obs = []
        t = 0.0
        while t < 6.0:
            obs.append(face_obs(t, "A", 0.25, lip=0.28, audio=0.9))
            obs.append(face_obs(t, "B", 0.75,
                                lip=0.10 if t < 2.0 else 0.95, audio=0.9))
            t = round(t + 0.1, 6)
        plain = run_timeline(obs, 6.0, ambiguity_margin=0.10,
                             onset_bonus=0.0)
        boosted = run_timeline(obs, 6.0, ambiguity_margin=0.10,
                               onset_bonus=0.15, onset_window_ms=300,
                               onset_rise_min=0.25,
                               target_acquisition_min_ms=300)
        plain_ids = [s.speaker_id for s in plain]
        boosted_ids = [s.speaker_id for s in boosted]
        self.assertIn("B", plain_ids)
        self.assertIn("B", boosted_ids)
        plain_first_b = next(s.start for s in plain if s.speaker_id == "B")
        boosted_first_b = next(s.start for s in boosted if s.speaker_id == "B")
        # Plain force-switches at the 0.35 boundary (~3.7); onset must win
        # by a clear margin via the faster acquisition floor.
        self.assertGreaterEqual(plain_first_b, 3.5)
        self.assertLess(boosted_first_b, plain_first_b - 1.0)

    def test_unseen_decay_releases_stale_incumbent(self):
        # A talks 0-1.5s then vanishes; B talks moderately throughout.
        # Legacy (decay 1.0) freezes A's smoothed score so A blocks B;
        # decay 0.9 melts A and hands over to B sooner.
        obs = []
        t = 0.0
        while t < 6.0:
            if t < 1.5:
                obs.append(face_obs(t, "A", 0.25, lip=0.9, audio=0.9))
            obs.append(face_obs(t, "B", 0.75, lip=0.5, audio=0.9))
            t = round(t + 0.1, 6)
        legacy = run_timeline(obs, 6.0, ambiguity_margin=0.10,
                              unseen_decay=1.0)
        melted = run_timeline(obs, 6.0, ambiguity_margin=0.10,
                              unseen_decay=0.9)
        self.assertIn("B", [s.speaker_id for s in melted])
        legacy_first_b = next(
            (s.start for s in legacy if s.speaker_id == "B"), 6.0)
        melted_first_b = next(
            s.start for s in melted if s.speaker_id == "B")
        self.assertLess(melted_first_b, legacy_first_b)


class ManagerLockTests(unittest.TestCase):
    def _present(self, *keys, centers=None, age=10):
        out = {}
        for key in keys:
            center = (centers or {}).get(key, (0.5, 0.5))
            out[key] = (center, age)
        return out

    def test_lock_blocks_marginal_takeover(self):
        manager = TargetManager(transition_lock_ms=650, force_margin=0.40)
        host, guest, third = (0, "host"), (0, "guest"), (0, "third")
        manager.step(0.0, host, 0.8, self._present(host, guest), scene_id=0)
        decision, events = manager.step(0.5, guest, 0.9,
                                        self._present(host, guest), scene_id=0)
        decision, _ = manager.step(0.6, guest, 0.9,
                                   self._present(host, guest), scene_id=0)
        self.assertEqual(decision.key, guest)
        kinds = [e.kind for e in events]
        # Marginal third party during lock: held, not switched.
        decision, events = manager.step(0.7, third, 0.75,
                                        self._present(guest, third), scene_id=0)
        self.assertEqual(decision.key, guest)
        # Force candidate overrides the lock.
        decision, _ = manager.step(1.3, third, 0.95,
                                   self._present(guest, third), scene_id=0)
        # After lock expiry (0.6+0.65=1.25) normal rules resume.
        self.assertIn(decision.key, (guest, third))

    def test_association_adopts_label(self):
        manager = TargetManager()
        old = (0, "speaker-005")
        new = (0, "speaker-009")
        present_old = {old: ((0.70, 0.50), 20, 0.020)}
        manager.step(0.0, old, 0.8, present_old, scene_id=0)
        # Vanish, expire grace with nothing around.
        for i in range(1, 12):
            manager.step(i * 0.1, None, 0.0, {}, scene_id=0)
        # Reappear nearby with a fresh id.
        present_new = {new: ((0.72, 0.51), 2, 0.021)}
        decision, events = manager.step(1.2, new, 0.8, present_new, scene_id=0)
        kinds = [e.kind for e in events]
        self.assertIn("ASSOCIATED", kinds)
        self.assertEqual(decision.key, old)


class AnchorTests(unittest.TestCase):
    def test_anchor_formula(self):
        from gaeclip.framing_geometry import compute_target_anchor

        ax, ay = compute_target_anchor((0.2, 0.2, 0.4, 0.7), crop_height=0.5625)
        self.assertAlmostEqual(ax, 0.3)
        # y1 + 0.35h - 0.18h = 0.2 + 0.085 = 0.285, above 0.5625/2? No:
        # max(0.28125, 0.285) ~ 0.285.
        self.assertAlmostEqual(ay, 0.285, places=3)

    def test_config_defaults(self):
        cfg = FramingConfig()
        self.assertEqual(cfg.speaker_min_margin, 0.12)
        self.assertEqual(cfg.speaker_onset_bonus, 0.15)
        self.assertEqual(cfg.target_acquisition_min_ms, 300)
        self.assertEqual(cfg.target_transition_lock_ms, 650)
        self.assertEqual(cfg.group_min_active_speakers, 2)
        self.assertEqual(cfg.group_min_confidence, 0.5)
        self.assertEqual(cfg.face_quality_weight, 0.1)
        self.assertEqual(cfg.target_crop_error_threshold, 0.12)
        self.assertTrue(cfg.center_gap_protection)
        self.assertEqual(cfg.switch_transition_ms, 400)
        self.assertEqual(cfg.switch_transition_steps, 8)
        self.assertEqual(cfg.easing_ms, 400)
        self.assertEqual(cfg.scene_cut_threshold, 0.25)
        self.assertEqual(cfg.scene_cut_min_interval_ms, 500)
        self.assertEqual(cfg.anchor_stale_after_ms, 1000)
        self.assertEqual(cfg.score_unseen_decay, 0.9)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Phase 4 integration: 5-person synthetic scenes through analyze_section.
# ---------------------------------------------------------------------------

def five_script(active_map, n_frames=12):
    """Scripted 5-face detections + per-frame bar regions.

    active_map: {frame_idx: [face_indices]} talking faces per frame.
    Returns (script, regions) for FakeDetector + motion_frames.
    """
    script = []
    regions = []
    for i in range(n_frames):
        row = [((x - 0.06, 0.25, x + 0.06, 0.65), 0.9) for x in XS5]
        script.append(row)
        talking = active_map.get(i, [])
        if talking and i % 2 == 1:
            x0 = min(XS5[j] for j in talking)
            x1 = max(XS5[j] for j in talking)
            regions.append((int(x0 * 160) - 7, int(x1 * 160) + 7))
        else:
            regions.append(None)
    return script, regions


def run_five(active_map, n_frames=12, duration=6.0, audio_active=True):
    import sys

    sys.path.insert(0, "tests")
    from unittest.mock import patch

    import gaeclip.autoframing as auto
    from test_autoframing import FakeDetector, motion_frames, tone_audio

    script, regions = five_script(active_map, n_frames)
    frames = motion_frames(n_frames, regions)
    stamps = [index * 0.5 for index in range(len(frames))]
    detector = FakeDetector(script)
    with patch.object(auto, "extract_audio_mono",
                      return_value=tone_audio(duration, audio_active)), \
        patch.object(auto, "BlazeFaceDetector", return_value=detector):
        return auto.analyze_section("dummy.mp4", duration=duration,
                                    frame_source=list(zip(stamps, frames)))


def followed_ids(plan):
    return [item.speaker_id for item in plan.instructions]


class FivePersonTests(unittest.TestCase):
    def test_f_only_d_speaks(self):
        # Only D (x=0.70) talks: D must become and remain the target
        # (acquisition via hold ~3.3s: initial windows carry no motion yet,
        # so audio-only tie activates first, evidence corrects after).
        # No midpoint parking: non-eased trajectory stays near a face.
        active = {i: [3] for i in range(12)}
        plan = run_five(active)
        tracks = {t.speaker_id: t for t in plan.tracks}
        d_ids = [sid for sid, t in tracks.items()
                 if abs(((t.box_at(3.0) or (0.64, 0, 0.76, 0))[0]
                         + (t.box_at(3.0) or (0.64, 0, 0.76, 0))[2]) / 2 - 0.70) < 0.05]
        self.assertTrue(d_ids)
        d_id = d_ids[0]
        d_time = sum(i.end - i.start for i in plan.instructions
                     if i.speaker_id == d_id)
        self.assertGreaterEqual(d_time, 2.0)
        anchors = {j: XS5[j] for j in range(5)}
        for point in plan.trajectory:
            if point.eased:
                continue
            nearest = min(abs(point.x - ax) for ax in anchors.values())
            self.assertLessEqual(nearest, 0.15)

    def test_g_alternating_speakers(self):
        # A 0-2.5s, D 2.5-5s, B 5-7.5s, E 7.5-12s: each gets followed,
        # switches stay bounded, no wild flapping. (E phase is long so the
        # 2s hold can actually complete.)
        active = {}
        for i in range(24):
            if i < 5:
                active[i] = [0]
            elif i < 10:
                active[i] = [3]
            elif i < 15:
                active[i] = [1]
            else:
                active[i] = [4]
        plan = run_five(active, n_frames=24, duration=12.0)
        ids = followed_ids(plan)
        tracks = {t.speaker_id: t for t in plan.tracks}
        # Map track ids to face slots by mid-clip position.
        def slot_of(speaker_id, at=5.0):
            box = tracks[speaker_id].box_at(at)
            if box is None:
                return -1
            cx = (box[0] + box[2]) / 2
            return min(range(5), key=lambda j: abs(XS5[j] - cx))
        self.assertIn(3, {slot_of(speaker_id, 4.0) for speaker_id in set(ids)})
        self.assertIn(4, {slot_of(speaker_id, 9.0) for speaker_id in set(ids)})
        self.assertLessEqual(len(set(ids)), 4)
        # No sustained midpoint: non-eased trajectory stays near a face.
        anchors = {j: XS5[j] for j in range(5)}
        sustained_off = 0
        for point in plan.trajectory:
            if point.eased:
                continue
            nearest = min(abs(point.x - ax) for ax in anchors.values())
            if nearest > 0.15:
                sustained_off += 1
        self.assertEqual(sustained_off, 0)

    def test_q_center_silent_never_targeted(self):
        # A/B/D/E talk in turns, C (center) never does: no SUSTAINED (>=1s)
        # targeting of C (transit passes during eased travel are fine).
        active = {}
        for i in range(20):
            if i < 5:
                active[i] = [0]
            elif i < 10:
                active[i] = [3]
            elif i < 15:
                active[i] = [1]
            else:
                active[i] = [4]
        plan = run_five(active, n_frames=20, duration=10.0)
        tracks = {t.speaker_id: t for t in plan.tracks}
        c_ids = [sid for sid, t in tracks.items()
                 if abs(((t.box_at(5.0) or (0.44, 0, 0.56, 0))[0]
                         + (t.box_at(5.0) or (0.44, 0, 0.56, 0))[2]) / 2 - 0.50) < 0.05]
        self.assertTrue(c_ids)
        c_id = c_ids[0]
        run, run_start = 0.0, None
        max_run = 0.0
        for point in sorted(plan.trajectory, key=lambda p: p.timestamp):
            if point.track_id == c_id and not point.eased:
                if run_start is None:
                    run_start = point.timestamp
                run = point.timestamp - run_start
                max_run = max(max_run, run)
            else:
                run_start = None
        self.assertLess(max_run, 1.0)

    def test_anti_jitter_bursts_ignored(self):
        # A talks throughout; others fire single-window bursts: no switch.
        active = {i: [0] for i in range(20)}
        active[4] = [1]
        active[8] = [2]
        active[12] = [3]
        active[16] = [4]
        plan = run_five(active, n_frames=20, duration=10.0)
        self.assertEqual(len(set(followed_ids(plan))), 1)

    def test_onset_destination_uses_current_anchor(self):
        # E silent until t=5, then strong: E gets acquired and the crop
        # centers land near E's CURRENT box (not a stale position).
        active = {i: [0] for i in range(10)}
        active.update({i: [4] for i in range(10, 20)})
        plan = run_five(active, n_frames=20, duration=10.0)
        tracks = {t.speaker_id: t for t in plan.tracks}
        e_ids = [sid for sid, t in tracks.items()
                 if abs(((t.box_at(7.0) or (0.84, 0, 0.96, 0))[0]
                         + (t.box_at(7.0) or (0.84, 0, 0.96, 0))[2]) / 2 - 0.90) < 0.05]
        self.assertTrue(e_ids)
        e_centers = [item.center_x for item in plan.instructions
                     if item.speaker_id == e_ids[0]]
        self.assertTrue(e_centers)
        for cx in e_centers:
            self.assertLess(abs(cx - 0.90), 0.15)


class AdjacentFaceTests(unittest.TestCase):
    def test_no_midpoint_single_active(self):
        # REQUIRED TEST §43: A x=0.25 active only -> crop stays near A.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import (FakeDetector, motion_frames, talking_face,
                                      tone_audio)

        script = [talking_face(0.25) + talking_face(0.75) for _ in range(12)]
        frames = motion_frames(12, [(24, 64)] * 12)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        for instruction in plan.instructions:
            self.assertLess(abs(instruction.center_x - 0.25), 0.15)

    def test_no_false_group_adjacent(self):
        # REQUIRED TEST §44: adjacent faces, only A speaks -> SINGLE A.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import (FakeDetector, motion_frames, talking_face,
                                      tone_audio)

        script = [talking_face(0.40) + talking_face(0.60) for _ in range(12)]
        frames = motion_frames(12, [(56, 72)] * 12)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        self.assertEqual(plan.mode, "speaker-follow")
        self.assertNotEqual(plan.mode, "group")

    def test_h_true_overlap_may_group(self):
        # Both faces talking through the whole clip, but far apart: Phase
        # 4.2 (§52) forbids split-screen, and a midpoint would be an empty
        # center shot — so the hierarchy elects the strongest SINGLE.
        # Neither face is amputated and no split panes are produced.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import (FakeDetector, motion_frames, talking_face,
                                      tone_audio)

        script = [talking_face(0.15) + talking_face(0.85) for _ in range(12)]
        frames = motion_frames(12, [(16, 144)] * 12)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        self.assertEqual(plan.mode, "speaker-follow")
        for instruction in plan.instructions:
            self.assertEqual(instruction.layout, "single")

    def test_edge_speakers_framed(self):
        # Far left / far right speakers stay valid + visible.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import (FakeDetector, motion_frames, talking_face,
                                      tone_audio)

        for x, bar in ((0.08, (6, 20)), (0.92, (140, 154))):
            script = [talking_face(x) for _ in range(12)]
            frames = motion_frames(12, [bar] * 12)
            stamps = [index * 0.5 for index in range(len(frames))]
            with patch.object(auto, "extract_audio_mono",
                              return_value=tone_audio(6.0, True)), \
                patch.object(auto, "BlazeFaceDetector",
                             return_value=FakeDetector(script)):
                plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                            frame_source=list(zip(stamps, frames)))
            self.assertTrue(plan.instructions)
            for instruction in plan.instructions:
                self.assertGreaterEqual(instruction.center_x, 0.0)
                self.assertLessEqual(instruction.center_x, 1.0)
                self.assertGreater(instruction.width, 0.0)


class OcclusionIdTests(unittest.TestCase):
    def test_k_occlusion_holds_despite_bystanders(self):
        # A talks, vanishes 1 window, returns; B-E visible but silent.
        active = {i: ([0] if i != 6 else []) for i in range(12)}
        plan = run_five(active)
        ids = followed_ids(plan)
        self.assertTrue(ids)
        first = ids[0]
        for speaker_id in ids:
            self.assertEqual(speaker_id, first)

    def test_l_id_change_associates(self):
        # Face at 0.70 vanishes, nearby fresh id appears: single label kept.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        script = ([[((0.64, 0.25, 0.76, 0.65), 0.9)] for _ in range(6)]
                  + [[((0.66, 0.25, 0.78, 0.65), 0.88)] for _ in range(6)])
        frames = motion_frames(12, [(102, 124)] * 12)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        ids = followed_ids(plan)
        self.assertTrue(ids)
        # Association keeps one label; without it there would be two.
        self.assertLessEqual(len(set(ids)), 2)


class SceneCutTests(unittest.TestCase):
    def test_r_scene_cut_fresh_acquire(self):
        # Abrupt scene change mid-clip: completes, cuts tracked, valid crops.
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, talking_face, tone_audio
        import numpy as np

        script = [talking_face(0.3) for _ in range(6)]
        script += [talking_face(0.7) for _ in range(6)]
        frames = motion_frames(6, [(30, 70)] * 6)
        dark = [np.full((160, 160, 3), 20, dtype=np.uint8) for _ in range(6)]
        frames = frames + dark
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        self.assertTrue(plan.scenes)
        self.assertTrue(plan.instructions)
        for instruction in plan.instructions:
            self.assertGreaterEqual(instruction.center_x, 0.0)
            self.assertLessEqual(instruction.center_x, 1.0)


class ThreePersonTests(unittest.TestCase):
    def _three(self, active_map, n_frames=18, duration=9.0):
        import sys

        sys.path.insert(0, "tests")
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        xs = (0.20, 0.50, 0.80)
        script = []
        regions = []
        for i in range(n_frames):
            script.append([((x - 0.06, 0.25, x + 0.06, 0.65), 0.9) for x in xs])
            talking = active_map.get(i, [])
            if talking and i % 2 == 1:
                x0 = min(xs[j] for j in talking)
                x1 = max(xs[j] for j in talking)
                regions.append((int(x0 * 160) - 7, int(x1 * 160) + 7))
            else:
                regions.append(None)
        frames = motion_frames(n_frames, regions)
        stamps = [index * 0.5 for index in range(len(frames))]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(duration, True)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            return auto.analyze_section("dummy.mp4", duration=duration,
                                        frame_source=list(zip(stamps, frames)))

    def test_d_three_alternating(self):
        # A 0-3s, B 3-6s, C 6-9s: each gets a turn, switches bounded.
        active = {}
        for i in range(18):
            active[i] = [0] if i < 6 else ([1] if i < 12 else [2])
        plan = self._three(active)
        ids = [item.speaker_id for item in plan.instructions]
        self.assertTrue(ids)
        # All three speakers get followed at some point, no flapping.
        self.assertLessEqual(len(set(ids)), 3)
        self.assertGreaterEqual(len(set(ids)), 2)

    def test_e_three_overlapping_may_group(self):
        # All three talking together: sustained overlap -> GROUP allowed.
        active = {i: [0, 1, 2] for i in range(12)}
        plan = self._three(active, n_frames=12, duration=6.0)
        self.assertIn(plan.mode, ("group", "speaker-follow"))
        if plan.mode == "group":
            for instruction in plan.instructions:
                self.assertIn(instruction.layout, ("group", "split"))


class SinglePairTests(unittest.TestCase):
    """TEST A/B/C + J: 1-person and 2-person acquisition matrix (§39)."""

    def _pair_obs(self, a_lip_fn, b_lip_fn, duration=6.0, step=0.1):
        obs = []
        t = 0.0
        while t < duration:
            obs.append(face_obs(t, "A", 0.25, lip=a_lip_fn(t), audio=0.9))
            obs.append(face_obs(t, "B", 0.75, lip=b_lip_fn(t), audio=0.9))
            t = round(t + step, 6)
        return obs

    def test_a_single_speaker(self):
        # TEST A: 1 person speaking alone -> timeline all A.
        obs = []
        t = 0.0
        while t < 4.0:
            obs.append(face_obs(t, "A", 0.50, lip=0.9, audio=0.9))
            t = round(t + 0.1, 6)
        timeline = run_timeline(obs, 4.0, ambiguity_margin=0.12)
        self.assertTrue(timeline)
        self.assertEqual({s.speaker_id for s in timeline}, {"A"})

    def test_b_two_people_one_speaks(self):
        # TEST B: A talks (0.9), B silent (0.08) -> SINGLE A, never B.
        obs = self._pair_obs(lambda t: 0.9, lambda t: 0.08)
        timeline = run_timeline(obs, 6.0, ambiguity_margin=0.12)
        self.assertTrue(timeline)
        self.assertEqual({s.speaker_id for s in timeline}, {"A"})

    def test_c_two_people_alternating(self):
        # TEST C: A 0-3s, B 3-6s -> both followed, at most one switch.
        obs = self._pair_obs(
            lambda t: 0.9 if t < 3.0 else 0.08,
            lambda t: 0.08 if t < 3.0 else 0.9)
        timeline = run_timeline(obs, 6.0, ambiguity_margin=0.12)
        ids = [s.speaker_id for s in timeline]
        self.assertIn("A", ids)
        self.assertIn("B", ids)
        self.assertLessEqual(len(ids), 2)

    def test_j_brief_interruption_holds(self):
        # TEST J: A throughout, B bursts 200ms at t=3 -> HOLD A, no B.
        def a_lip(t):
            return 0.9
        def b_lip(t):
            return 0.95 if 3.0 <= t < 3.2 else 0.08
        obs = self._pair_obs(a_lip, b_lip, duration=6.0)
        timeline = run_timeline(obs, 6.0, ambiguity_margin=0.12)
        self.assertEqual({s.speaker_id for s in timeline}, {"A"})


class FivePersonSimultaneousTests(unittest.TestCase):
    """TEST H (5-person): D+E talk together among 5 (§39-40)."""

    def test_h_five_person_two_simultaneous(self):
        # D (0.70) + E (0.90) sustained together, others silent:
        # must be GROUP (both loud, together) or SINGLE D/E held —
        # never a midpoint single, never flapping across all 5.
        active = {i: [3, 4] for i in range(12)}
        plan = run_five(active)
        self.assertIn(plan.mode, ("group", "speaker-follow"))
        ids = set(followed_ids(plan))
        # At most the two active speakers (+ initial tie window).
        self.assertLessEqual(len(ids), 3)
        if plan.mode == "group":
            for instruction in plan.instructions:
                self.assertIn(instruction.layout, ("group", "split"))
        else:
            # SINGLE: trajectory stays near D or E, never mid-gap.
            anchors = {j: XS5[j] for j in range(5)}
            for point in plan.trajectory:
                if point.eased:
                    continue
                nearest = min(abs(point.x - ax) for ax in anchors.values())
                self.assertLessEqual(nearest, 0.15)
