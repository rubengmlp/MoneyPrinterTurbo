import json
import tempfile
import unittest
from pathlib import Path

import topics


class TestTopicParsing(unittest.TestCase):
    def test_iso8601_duration_parser(self):
        self.assertEqual(topics.parse_iso8601_duration("PT45S"), 45)
        self.assertEqual(topics.parse_iso8601_duration("PT1M2S"), 62)
        self.assertEqual(topics.parse_iso8601_duration("PT1H2M3S"), 3723)
        self.assertEqual(topics.parse_iso8601_duration("P1DT30S"), 86430)
        self.assertEqual(topics.parse_iso8601_duration(""), 0)
        self.assertEqual(topics.parse_iso8601_duration("garbage"), 0)

    def test_clean_title_removes_format_noise(self):
        cleaned = topics.clean_title(
            "This AI tool replaced my workflow #shorts [FULL GUIDE] | TechChannel"
        )
        self.assertEqual(cleaned, "This AI tool replaced my workflow")

    def test_clean_title_keeps_meaningful_text(self):
        self.assertEqual(
            topics.clean_title("How ChatGPT Can Automate Your Email"),
            "How ChatGPT Can Automate Your Email",
        )

    def test_clean_title_truncates_long_titles(self):
        cleaned = topics.clean_title("word " * 40)
        self.assertLessEqual(len(cleaned), 90)
        self.assertTrue(cleaned.endswith("..."))

    def test_monetizable_filter_rejects_clear_spam(self):
        self.assertFalse(topics.is_monetizable_subject("iPhone 17 GIVEAWAY"))
        self.assertFalse(topics.is_monetizable_subject("Funny cats compilation"))
        self.assertTrue(topics.is_monetizable_subject("How AI agents work"))


class TestScoring(unittest.TestCase):
    @staticmethod
    def _candidate(**overrides):
        values = {
            "subject": "How AI agents work",
            "source": "search",
            "views": 100_000,
            "duration_seconds": 45,
            "age_hours": 48,
        }
        values.update(overrides)
        # Engagement realista: ~5% de likes y ~0,5% de comentarios sobre vistas.
        values.setdefault("likes", int(values["views"] * 0.05))
        values.setdefault("comments", int(values["views"] * 0.005))
        return topics.Candidate(**values)

    def test_shorts_get_a_bonus(self):
        short = self._candidate(duration_seconds=45)
        long = self._candidate(duration_seconds=600)
        self.assertGreater(
            topics.score_candidate(short, 1.0, 14),
            topics.score_candidate(long, 1.0, 14),
        )

    def test_fresher_videos_score_higher(self):
        fresh = self._candidate(age_hours=12)
        stale = self._candidate(age_hours=14 * 24)
        self.assertGreater(
            topics.score_candidate(fresh, 1.0, 14),
            topics.score_candidate(stale, 1.0, 14),
        )

    def test_more_views_score_higher(self):
        popular = self._candidate(views=1_000_000)
        small = self._candidate(views=10_000)
        self.assertGreater(
            topics.score_candidate(popular, 1.0, 14),
            topics.score_candidate(small, 1.0, 14),
        )

    def test_niche_multiplier_scales_score(self):
        candidate = self._candidate()
        self.assertGreater(
            topics.score_candidate(candidate, 1.8, 14),
            topics.score_candidate(candidate, 1.0, 14),
        )

    def test_rank_deduplicates_similar_subjects(self):
        first = self._candidate(subject="How AI agents work", views=50_000)
        second = self._candidate(subject="How AI agents work #shorts", views=900_000)
        ranked = topics.rank_candidates([first, second], 1.0, 14)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].views, 900_000)

    def test_rank_drops_banned_subjects(self):
        ranked = topics.rank_candidates(
            [self._candidate(subject="AI iPhone GIVEAWAY 2026")], 1.0, 14
        )
        self.assertEqual(ranked, [])


class TestManifest(unittest.TestCase):
    def test_manifest_tasks_use_valid_cli_fields(self):
        candidate = topics.Candidate(subject="How AI agents work", source="search")
        tasks = topics.build_manifest_tasks(
            [candidate], language="en-US", aspect="9:16", paragraph_number=2
        )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["video_subject"], "How AI agents work")
        self.assertEqual(tasks[0]["video_language"], "en-US")
        self.assertEqual(tasks[0]["video_aspect"], "9:16")
        self.assertEqual(tasks[0]["paragraph_number"], 2)
        # Solo campos que VideoParams acepta; un typo haría fallar --batch-file.
        self.assertEqual(
            set(tasks[0]),
            {"video_subject", "video_language", "video_aspect", "paragraph_number"},
        )

    def test_manifest_is_capped_at_100_tasks(self):
        candidates = [
            topics.Candidate(subject=f"Topic {index}", source="search")
            for index in range(150)
        ]
        tasks = topics.build_manifest_tasks(
            candidates, language="en-US", aspect="9:16", paragraph_number=1
        )
        self.assertEqual(len(tasks), 100)

    def test_write_manifest_round_trips_utf8(self):
        tasks = [{"video_subject": "Inteligencia artificial", "video_language": "es-ES"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tasks.json"
            topics.write_manifest(str(path), tasks)
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, tasks)


if __name__ == "__main__":
    unittest.main()
