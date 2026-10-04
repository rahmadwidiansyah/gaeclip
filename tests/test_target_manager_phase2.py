"""Phase 2 tests: speaker score, explicit target machine, stability.

Score tests are pure (no mocks). Timeline tests call
``build_speaker_timeline`` directly. Manager tests drive ``TargetManager``
with synthetic score/presence streams. Analyze-level tests reuse the
mocked pipeline seam for mode preservation (group/static intact).
"""

import unittest

from gaeclip.speaker_score import (
    ScoreWeights,
    calculate_speaker_score,
    continuity_from_boxes,
    normalize_score,
    spatial_prior_for,
)
from gaeclip.target_manager import (
    CANDIDATE,
    NO_TARGET,
    OCCLUDED,
    SWITCHING,
    TRACKING,
    TargetManager,
)
from gaeclip.visual import BoundingBox, FaceObservation, build_speaker_timeline


def observation(timestamp, speaker, lip=0.9, audio=0.9, x=0.5, continuity=0.0):
    return FaceObservation(
        timestamp=timestamp, speaker_id=speaker,
        face=BoundingBox(x=max(0, x - 0.1), y=0.2, width=0.2, height=0.3),
        lip_score=lip, audio_score=audio, continuity_score=continuity,
    )


def presence_at(*keys, centers=None, age=10):
    out = {}
    for index, key in enumerate(keys):
        center = (centers or {}).get(key, (0.5, 0.5))
        out[key] = (center, age - index)
    return out


class ScoreTests(unittest.TestCase):
    def test_normalize_bounds(self):
        self.assertEqual(normalize_score(2.0), 1.0)
        self.assertEqual(normalize_score(-1.0), 0.0)
        self.assertEqual(normalize_score(float("nan")), 0.0)
        self.assertEqual(normalize_score("junk"), 0.0)
        self.assertEqual(normalize_score(0.42), 0.42)

    def test_fusion_baseline_weights(self):
        score = calculate_speaker_score(0.9, 0.9, 0.0)
        self.assertAlmostEqual(score, 0.55 * 0.9 + 0.35 * 0.9, places=6)

    def test_weights_normalize(self):
        score = calculate_speaker_score(
            1.0, 1.0, 1.0, weights=ScoreWeights(2.0, 1.0, 1.0))
        self.assertAlmostEqual(score, 1.0)

    def test_j_big_face_silent_loses_to_small_talker(self):
        big_silent = calculate_speaker_score(0.05, 0.05, 0.9)
        small_talker = calculate_speaker_score(0.9, 0.9, 0.2)
        self.assertGreater(small_talker, big_silent)
        # Size appears nowhere in the score: identical components tie
        # regardless of bounding-box area (position never votes).

    def test_k_audio_only_fuses_mid(self):
        only_audio = calculate_speaker_score(0.1, 0.9, 0.0)
        both_low = calculate_speaker_score(0.1, 0.1, 0.0)
        both_high = calculate_speaker_score(0.9, 0.9, 0.0)
        self.assertGreater(only_audio, both_low)
        self.assertLess(only_audio, both_high)

    def test_l_visual_only_fuses_mid(self):
        only_visual = calculate_speaker_score(0.9, 0.1, 0.0)
        both_low = calculate_speaker_score(0.1, 0.1, 0.0)
        both_high = calculate_speaker_score(0.9, 0.9, 0.0)
        self.assertGreater(only_visual, both_low)
        self.assertLess(only_visual, both_high)

    def test_m_continuity_breaks_ties(self):
        young = calculate_speaker_score(0.8, 0.8, 0.0)
        mature = calculate_speaker_score(0.8, 0.8, 1.0)
        self.assertGreater(mature, young)

    def test_continuity_decay(self):
        boxes = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
        fresh = continuity_from_boxes(boxes, 2.5)
        self.assertAlmostEqual(fresh, 1.0)
        stale = continuity_from_boxes(boxes, 5.0)
        self.assertLess(stale, 0.2)
        self.assertEqual(continuity_from_boxes([], 1.0), 0.0)

    def test_spatial_neutral_by_default(self):
        self.assertEqual(spatial_prior_for(0.05, 0.05), 1.0)
        biased = spatial_prior_for(0.05, 0.05, enabled=True)
        self.assertGreaterEqual(biased, 0.85)
        self.assertLessEqual(biased, 1.0)
        self.assertEqual(spatial_prior_for(0.5, 0.5, enabled=True), 1.0)


class TimelineMarginTests(unittest.TestCase):
    def test_b_marginal_candidate_no_switch(self):
        # A ~= 0.72 sustained; B ~= 0.765 sustained (gap 0.045 < 0.10).
        observations = [
            *[observation(index / 10, "host", lip=0.8, audio=0.8) for index in range(0, 51)],
            *[observation(index / 10, "guest", lip=0.85, audio=0.85) for index in range(10, 51)],
        ]
        timeline = build_speaker_timeline(observations, duration=5, sample_step=0.1,
                                          switch_margin=0.10)
        self.assertEqual([item.speaker_id for item in timeline], ["host"])

    def test_c_strong_candidate_switches_after_hold(self):
        # A ~= 0.585; B ~= 0.855 from t=1 (gap 0.27 clears 0.10 + 2s hold).
        observations = [
            *[observation(index / 10, "host", lip=0.65, audio=0.65) for index in range(0, 61)],
            *[observation(index / 10, "guest", lip=0.95, audio=0.95) for index in range(10, 61)],
        ]
        timeline = build_speaker_timeline(observations, duration=6, sample_step=0.1,
                                          switch_margin=0.10)
        self.assertEqual([item.speaker_id for item in timeline], ["host", "guest"])
        # Switch DECISION at ~=2.9 (candidate start is backdated by design:
        # rendering follows reality, see analyze_section partition comment).
        self.assertGreaterEqual(timeline[0].end, 2.9)

    def test_d_200ms_blip_no_switch(self):
        observations = [
            *[observation(index / 10, "host") for index in range(0, 51)],
            *[observation(index / 10, "guest") for index in range(20, 22)],
        ]
        timeline = build_speaker_timeline(observations, duration=5, sample_step=0.1,
                                          switch_margin=0.10)
        self.assertEqual([item.speaker_id for item in timeline], ["host"])

    def test_tie_break_deterministic_incumbent_wins(self):
        first = [observation(0.0, "beta"), observation(0.0, "alpha"),
                 observation(0.5, "beta"), observation(0.5, "alpha")]
        second = [observation(0.0, "alpha"), observation(0.0, "beta"),
                  observation(0.5, "alpha"), observation(0.5, "beta")]
        one = build_speaker_timeline(first, duration=1, sample_step=0.1)
        two = build_speaker_timeline(second, duration=1, sample_step=0.1)
        self.assertEqual([s.speaker_id for s in one], [s.speaker_id for s in two])
        self.assertEqual(one[0].speaker_id, "alpha")  # sorted order, not hash order


class ManagerTests(unittest.TestCase):
    def test_a_stable_speaker_tracking(self):
        manager = TargetManager()
        key = (0, "host")
        states = set()
        for step in range(20):
            decision, _ = manager.step(step * 0.1, key, 0.8,
                                       presence_at(key), scene_id=0)
            states.add(decision.state)
        self.assertEqual(states, {TRACKING})

    def test_occluded_freezes_last_live_position(self):
        # Regression: on LOST the frozen center must be the last LIVE
        # framing, never the (0.5, 0.5) default (which produced a phantom
        # jump that only smoothing hid).
        manager = TargetManager(lost_grace_s=0.7)
        host = (0, "host")
        live = {(0, "host"): ((0.3, 0.4), 10)}
        manager.step(0.0, host, 0.8, live, scene_id=0)
        decision, _ = manager.step(0.5, host, 0.8, {}, scene_id=0)
        self.assertEqual(decision.state, OCCLUDED)
        self.assertEqual(decision.center, (0.3, 0.4))

    def test_e_occluded_restore_in_grace(self):
        manager = TargetManager(lost_grace_s=0.7)
        host, other = (0, "host"), (0, "other")
        decision, _ = manager.step(0.0, host, 0.8, presence_at(host, other), scene_id=0)
        self.assertEqual(decision.state, TRACKING)
        kinds = []
        for step in range(1, 4):  # host gone 0.3s
            decision, events = manager.step(step * 0.1, host, 0.8,
                                            presence_at(other), scene_id=0)
            kinds.extend(e.kind for e in events)
        self.assertIn("LOST", kinds)
        self.assertEqual(decision.state, OCCLUDED)
        # Same-epoch return inside grace restores the SAME target.
        decision, events = manager.step(0.4, host, 0.8,
                                        presence_at(host, other), scene_id=0)
        self.assertEqual(decision.state, TRACKING)
        self.assertEqual(decision.key, host)
        self.assertIn("RESTORE", [e.kind for e in events])

    def test_f_grace_expired_no_target_then_fallback(self):
        manager = TargetManager(lost_grace_s=0.7)
        host, other = (0, "host"), (0, "other")
        manager.step(0.0, host, 0.8, presence_at(host, other), scene_id=0)
        last = None
        kinds = []
        for step in range(1, 10):  # host gone 0.9s > grace
            last, events = manager.step(step * 0.1, host, 0.8,
                                        presence_at(other), scene_id=0)
            kinds.extend(e.kind for e in events)
        self.assertIn("GRACE_EXPIRED", kinds)
        self.assertIn("FALLBACK", kinds)
        self.assertEqual(last.state, TRACKING)
        self.assertEqual(last.kind, "fallback")
        self.assertEqual(last.key, other)

    def test_f_empty_scene_static(self):
        manager = TargetManager()
        decision, _ = manager.step(0.0, None, 0.0, {}, scene_id=0)
        self.assertEqual(decision.state, NO_TARGET)
        self.assertEqual(decision.kind, "static")

    def test_g_long_absence_bounded_then_reacquire(self):
        manager = TargetManager(lost_grace_s=0.7)
        host = (0, "host")
        manager.step(0.0, host, 0.8, presence_at(host), scene_id=0)
        states = []
        for step in range(1, 31):  # gone 3s
            decision, _ = manager.step(step * 0.1, host, 0.0, {}, scene_id=0)
            states.append(decision.state)
        # Bounded: occlusion, then explicit NO_TARGET/static (never frozen).
        self.assertIn(OCCLUDED, states)
        self.assertIn(NO_TARGET, states)
        self.assertEqual(states[-1], NO_TARGET)
        # Return re-acquires as a fresh TRACKING (same key, new decision).
        decision, events = manager.step(3.1, host, 0.8, presence_at(host), scene_id=0)
        self.assertEqual(decision.state, TRACKING)
        self.assertEqual(decision.key, host)
        self.assertIn("ACQUIRE", [e.kind for e in events])

    def test_h_scene_cut_resets_candidate(self):
        manager = TargetManager()
        host, guest = (0, "host"), (0, "guest")
        manager.step(0.0, host, 0.8, presence_at(host, guest), scene_id=0)
        decision, events = manager.step(0.5, guest, 0.95, presence_at(host, guest), scene_id=0)
        self.assertEqual(decision.state, CANDIDATE)
        decision, events = manager.step(1.0, guest, 0.95, presence_at(host, guest), scene_id=1)
        kinds = [e.kind for e in events]
        self.assertIn("SCENE_RESET", kinds)
        # Fresh evaluation: stale candidate is gone, never a blind switch.
        self.assertIsNone(manager.candidate)
        self.assertNotEqual(decision.state, SWITCHING)

    def test_i_epoch_reuse_is_distinct_identity(self):
        manager = TargetManager(lost_grace_s=0.7)
        old, new = (4, "speaker-002"), (5, "speaker-002")
        decision, _ = manager.step(0.0, old, 0.8, presence_at(old), scene_id=0)
        self.assertEqual(decision.key, old)
        # Same numeric id, new epoch, new scene: must NOT restore the old one.
        decision, events = manager.step(1.0, new, 0.8, presence_at(new), scene_id=1)
        self.assertNotEqual(decision.key, old)
        self.assertNotIn("RESTORE", [e.kind for e in events])

    def test_n_no_face_no_target(self):
        manager = TargetManager()
        for step in range(5):
            decision, _ = manager.step(step * 0.5, None, 0.0, {}, scene_id=0)
            self.assertEqual(decision.state, NO_TARGET)
            self.assertEqual(decision.kind, "static")

    def test_manager_follows_timeline_switch_presence_gated(self):
        # Corrected semantics (live-test root cause): margin/hold are
        # enforced by the timeline on live per-window scores. The manager
        # must NOT re-gate timeline switches against stale segment
        # aggregates (that blocked legitimate switches); it gates on
        # PRESENCE: present candidate -> switch, absent -> hold incumbent.
        manager = TargetManager(switch_margin=0.10)
        host, guest = (0, "host"), (0, "guest")
        manager.step(0.0, host, 0.70, presence_at(host, guest), scene_id=0)
        # Confident timeline handover to a PRESENT candidate switches.
        decision, events = manager.step(0.1, guest, 0.74,
                                        presence_at(host, guest), scene_id=0)
        self.assertEqual(decision.state, CANDIDATE)
        decision, _ = manager.step(0.2, guest, 0.74,
                                   presence_at(host, guest), scene_id=0)
        self.assertEqual(decision.key, guest)
        # Same handover, candidate ABSENT: incumbent holds.
        manager2 = TargetManager(switch_margin=0.10)
        manager2.step(0.0, host, 0.70, presence_at(host, guest), scene_id=0)
        decision, _ = manager2.step(0.1, guest, 0.95,
                                    presence_at(host), scene_id=0)
        self.assertEqual(decision.key, host)
        self.assertEqual(decision.state, TRACKING)


class GroupModeTests(unittest.TestCase):
    def test_o_group_path_intact(self):
        from unittest.mock import patch

        import gaeclip.autoframing as auto
        from test_autoframing import FakeDetector, motion_frames, tone_audio

        script = [[((0.15, 0.2, 0.35, 0.7), 0.9), ((0.65, 0.2, 0.85, 0.7), 0.9)]
                  for _ in range(12)]
        frames = motion_frames(12, [None] * 12)
        stamps = [index * 0.5 for index in range(12)]
        with patch.object(auto, "extract_audio_mono",
                          return_value=tone_audio(6.0, False)), \
            patch.object(auto, "BlazeFaceDetector",
                         return_value=FakeDetector(script)):
            plan = auto.analyze_section("dummy.mp4", duration=6.0,
                                        frame_source=list(zip(stamps, frames)))
        # Mode-level decision: two silent far-apart faces. Phase 4.2 (§52)
        # forbids split-screen, and the spread cannot share one correct
        # 9:16 window without an empty-center shot — so the hierarchy
        # elects a single stable face (never amputating, never split).
        self.assertEqual(plan.mode, "speaker-follow")
        self.assertTrue(plan.instructions)
        # Geometry-level: no split panes, no empty-center midpoint.
        for instruction in plan.instructions:
            self.assertEqual(instruction.layout, "single")


if __name__ == "__main__":
    unittest.main()
