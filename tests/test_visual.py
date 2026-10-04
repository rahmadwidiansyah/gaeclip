import unittest

from gaeclip.visual import (
    BoundingBox,
    FaceObservation,
    build_speaker_timeline,
    make_crop_instructions,
)


class VisualPipelineTests(unittest.TestCase):
    def observation(self, timestamp, speaker, lip=0.9, audio=0.9, x=0.5):
        return FaceObservation(
            timestamp=timestamp, speaker_id=speaker,
            face=BoundingBox(x=max(0, x - 0.1), y=0.2, width=0.2, height=0.3),
            lip_score=lip, audio_score=audio,
        )

    def test_short_interruption_does_not_switch_target(self):
        observations = [
            *[self.observation(index / 10, "host") for index in range(0, 51)],
            *[self.observation(index / 10, "guest") for index in range(20, 25)],
        ]
        timeline = build_speaker_timeline(observations, duration=5, sample_step=0.1)
        self.assertEqual([(item.start, item.end, item.speaker_id) for item in timeline], [(0, 5, "host")])

    def test_switch_requires_two_seconds(self):
        observations = [
            *[self.observation(index / 10, "host") for index in range(0, 21)],
            *[self.observation(index / 10, "guest", lip=1, audio=1) for index in range(21, 51)],
        ]
        timeline = build_speaker_timeline(observations, duration=5, sample_step=0.1)
        self.assertEqual([item.speaker_id for item in timeline], ["host", "guest"])
        self.assertGreaterEqual(timeline[1].start, 2.1)

    def test_crop_is_portrait_and_in_frame(self):
        observations = [self.observation(0, "host", x=0.9)]
        timeline = build_speaker_timeline(observations, duration=1, sample_step=0.1)
        crops = make_crop_instructions(timeline, observations)
        self.assertEqual(crops[0].layout, "single")
        self.assertLessEqual(crops[0].center_x + crops[0].width / 2, 1)

    def test_split_layout_removed_far_pair_becomes_single(self):
        # Phase 4.2 (§52): split-screen removed. Far-apart faces must NOT
        # produce split panes — the leader gets a single comfortable crop.
        from gaeclip.visual import make_layout_instructions
        observations = [
            self.observation(0, "host", x=0.25),
            self.observation(0, "guest", x=0.75),
        ]
        timeline = build_speaker_timeline(observations, duration=1, sample_step=0.1)
        layout = make_layout_instructions(timeline, observations)
        self.assertTrue(layout)
        self.assertTrue(all(item.layout != "split" for item in layout))

    def test_close_pair_gets_single_comfortable_crop(self):
        from gaeclip.visual import make_layout_instructions
        observations = [
            self.observation(0, "host", x=0.4),
            self.observation(0, "guest", x=0.6),
        ]
        timeline = build_speaker_timeline(observations, duration=1, sample_step=0.1)
        layout = make_layout_instructions(timeline, observations)
        self.assertTrue(layout)
        self.assertTrue(all(item.layout == "single" for item in layout))
        # Both faces inside with margin (no edge cut, no empty center).
        for item in layout:
            left = item.center_x - item.width / 2
            right = item.center_x + item.width / 2
            self.assertLessEqual(left, 0.3 - 0.02)
            self.assertGreaterEqual(right, 0.7 + 0.02)


if __name__ == "__main__":
    unittest.main()
