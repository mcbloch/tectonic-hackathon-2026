"""Tests for the logic the demo cannot live without."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from reasoner.__main__ import main
from reasoner.apply import apply_updates
from reasoner.drift import EPSILON, evaluate_document
from reasoner.graph import build_clusters, build_tag_graph, choose_k
from reasoner.loader import canonical_tag, load_documents, load_state, save_state, select_batch
from reasoner.watcher import diff_snapshots, snapshot, watch

BASE = Path(__file__).resolve().parent.parent
CORPUS = BASE / "fixtures" / "corpus"
INCOMING = BASE / "fixtures" / "incoming"


class FixturePipelineTests(unittest.TestCase):
    """The demo path: corpus -> logical graph -> clusters -> drift reasoning."""

    @classmethod
    def setUpClass(cls):
        cls.documents, cls.errors = load_documents([CORPUS, INCOMING])
        cls.corpus = [doc for doc in cls.documents if CORPUS.resolve() in doc.path.parents]
        cls.incoming = [doc for doc in cls.documents if INCOMING.resolve() in doc.path.parents]
        extra = [tag for doc in cls.incoming for tag in doc.tags]
        cls.tag_graph = build_tag_graph(cls.corpus, extra_tags=extra)
        cls.clusters = build_clusters(cls.corpus, cls.tag_graph)
        cls.evaluations = {
            doc.path.name: evaluate_document(doc, "new", cls.tag_graph, cls.clusters, EPSILON)
            for doc in cls.incoming
        }

    def test_all_fixtures_load_with_both_schemas(self):
        self.assertEqual([], self.errors)
        self.assertEqual(14, len(self.corpus))
        self.assertEqual(4, len(self.incoming))
        sources = {doc.source for doc in self.documents}
        self.assertEqual({"outlook", "microsoft_teams"}, sources)

    def test_graph_has_dimensions_and_clusters(self):
        self.assertGreaterEqual(len(self.tag_graph.dimensions), 2)
        self.assertGreaterEqual(len(self.clusters), 2)
        self.assertEqual(choose_k(14), len(self.clusters))
        for cluster in self.clusters:
            self.assertTrue(cluster.label)
            self.assertTrue(cluster.dimension_tags)

    def test_clean_document_stays_within_epsilon(self):
        evaluation = self.evaluations["teams_proximus_cutoff_2026-09.json"]
        self.assertFalse(evaluation.drifting)
        self.assertFalse(evaluation.ambiguous)
        self.assertFalse(evaluation.tag_conflict)
        self.assertEqual((), evaluation.suggestions)
        self.assertLess(evaluation.nearest.distance, EPSILON)

    def test_single_tag_conflict_is_detected_even_within_epsilon(self):
        evaluation = self.evaluations["outlook_randstad_belgium_2026-09.json"]
        self.assertLess(evaluation.nearest.distance, EPSILON)
        self.assertTrue(evaluation.ambiguous)
        self.assertEqual("randstad", evaluation.drifting_tag)
        self.assertNotEqual(evaluation.removal_cluster, evaluation.nearest.cluster.id)

    def test_mixed_context_document_drifts(self):
        evaluation = self.evaluations["outlook_proximus_netherlands_2026-09.json"]
        self.assertTrue(evaluation.drifting)
        self.assertGreater(evaluation.nearest.distance, EPSILON)

    def test_unseen_tag_drift_is_attributed_to_that_tag(self):
        evaluation = self.evaluations["teams_proximus_offboarding_2026-09.json"]
        self.assertTrue(evaluation.drifting)
        self.assertEqual("offboarding", evaluation.drifting_tag)
        self.assertGreater(evaluation.drifting_relief, 0.3)
        self.assertLess(evaluation.removal_distance, EPSILON)

    def test_suggestion_replaces_wrong_project_tag(self):
        evaluation = self.evaluations["outlook_randstad_belgium_2026-09.json"]
        self.assertTrue(evaluation.suggestions)
        suggestion = evaluation.suggestions[0]
        self.assertEqual("proximus", suggestion.tag)
        self.assertTrue(suggestion.fits)
        self.assertLess(suggestion.simulated_distance, EPSILON)
        self.assertGreater(suggestion.role_similarity, 0.0)
        self.assertGreater(suggestion.native_support, 0.5)

    def test_unseen_tag_suggestion_stays_inside_cluster(self):
        evaluation = self.evaluations["teams_proximus_offboarding_2026-09.json"]
        self.assertTrue(evaluation.suggestions)
        suggestion = evaluation.suggestions[0]
        self.assertTrue(suggestion.fits)
        self.assertLess(suggestion.simulated_distance, EPSILON)
        self.assertGreater(suggestion.native_support, 0.0)
        self.assertEqual(evaluation.removal_cluster, suggestion.simulated_cluster)

    def test_distances_are_bounded_and_ordered(self):
        for evaluation in self.evaluations.values():
            self.assertLessEqual(evaluation.nearest.distance, evaluation.runner_up.distance)
            for _, distance in evaluation.distances:
                self.assertGreaterEqual(distance, 0.0)
                self.assertLessEqual(distance, 1.0)

    def test_cli_report_and_drift_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            exit_code = main([
                "--database", str(CORPUS),
                "--database", str(INCOMING),
                "--since", "2026-09-15",
                "--json", str(report),
                "--quiet",
                "--dry-run",
                "--fail-on-drift",
            ])
            self.assertEqual(1, exit_code)
            payload = json.loads(report.read_text())
            self.assertEqual(4, payload["counts"]["evaluated"])
            self.assertEqual(2, payload["counts"]["drifting"])
            names = {Path(item["path"]).name for item in payload["files"]}
            self.assertIn("teams_proximus_offboarding_2026-09.json", names)


class FreshnessTests(unittest.TestCase):
    """Newly created, edited and touched files drive what gets evaluated."""

    def test_new_edited_and_touched_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(CORPUS / "teams_proximus_cutoff_2026-07.json", root / "doc.json")
            state_path = root / "state.json"

            documents, errors = load_documents([root])
            self.assertEqual([], errors)
            batch = select_batch(documents, load_state(state_path))
            self.assertEqual(["new"], [item.status for item in batch])

            save_state(state_path, documents)
            batch = select_batch(load_documents([root])[0], load_state(state_path))
            self.assertEqual([], batch)

            target = root / "doc.json"
            payload = json.loads(target.read_text())
            payload["client_tags"].append("retail")
            target.write_text(json.dumps(payload))
            batch = select_batch(load_documents([root])[0], load_state(state_path))
            self.assertEqual(["edited"], [item.status for item in batch])

            save_state(state_path, load_documents([root])[0])
            stat = target.stat()
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            batch = select_batch(load_documents([root])[0], load_state(state_path))
            self.assertEqual(["touched"], [item.status for item in batch])

    def test_invalid_document_is_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.json").write_text("{not json")
            (root / "missing_tags.json").write_text(json.dumps({"source": "outlook", "type": "email", "sent_at": "2026-09-01"}))
            documents, errors = load_documents([root])
            self.assertEqual([], documents)
            self.assertEqual(2, len(errors))


class WatcherTests(unittest.TestCase):
    """The watcher must notice created, modified and removed documents."""

    def test_snapshot_and_diff_detects_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.json").write_text("{}")
            before = snapshot([root])

            (root / "b.json").write_text("{}")
            after = snapshot([root])
            added, modified, removed = diff_snapshots(before, after)
            self.assertEqual([str((root / "b.json").resolve())], added)
            self.assertEqual([], modified)
            self.assertEqual([], removed)

            (root / "a.json").write_text('{"edited": true}')
            (root / "b.json").unlink()
            final = snapshot([root])
            added, modified, removed = diff_snapshots(after, final)
            self.assertEqual([], added)
            self.assertEqual([str((root / "a.json").resolve())], modified)
            self.assertEqual([str((root / "b.json").resolve())], removed)

    def test_watch_runs_once_per_change_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.json").write_text("{}")
            runs: list[int] = []
            pulses = {"count": 0}

            def fake_sleep(seconds: float) -> None:
                if seconds != 0.01:  # debounce sleeps are not counted
                    return
                pulses["count"] += 1
                if pulses["count"] == 1:
                    (root / "b.json").write_text("{}")
                elif pulses["count"] >= 3:
                    raise KeyboardInterrupt

            exit_code = watch(
                [root],
                lambda: runs.append(1) or 0,
                interval=0.01,
                debounce=0.001,
                out=lambda *_: None,
                sleep=fake_sleep,
            )
            self.assertEqual(0, exit_code)
            self.assertEqual(2, len(runs))  # initial run + one run for the change


class ApplyTests(unittest.TestCase):
    """Applying a correction must rewrite the tag and keep the old one."""

    def test_apply_replaces_odd_tag_and_logs_the_old_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "database"
            database.mkdir()
            shutil.copy(INCOMING / "teams_proximus_offboarding_2026-09.json", database / "new.json")

            documents, errors = load_documents([CORPUS, database])
            self.assertEqual([], errors)
            corpus = [doc for doc in documents if CORPUS.resolve() in doc.path.parents]
            new_doc = next(doc for doc in documents if doc.path.parent == database.resolve())
            graph = build_tag_graph(corpus, extra_tags=new_doc.tags)
            clusters = build_clusters(corpus, graph)
            evaluation = evaluate_document(new_doc, "new", graph, clusters, EPSILON)
            self.assertTrue(evaluation.drifting)

            notif = root / "notif"
            notif.write_text("retail\n")  # stale content must be replaced
            applied, apply_errors = apply_updates([evaluation], notif)
            self.assertEqual([], apply_errors)
            self.assertEqual([str(new_doc.path)], applied)

            updated = json.loads((database / "new.json").read_text())
            tags = [canonical_tag(tag) for tag in updated["client_tags"]]
            self.assertNotIn("offboarding", tags)
            self.assertIn(canonical_tag(evaluation.suggestions[0].tag), tags)

            expected = "offboarding"  # only the old tag, nothing else
            self.assertEqual([expected], notif.read_text().splitlines())

    def test_bootstrap_runs_never_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "database"
            database.mkdir()
            shutil.copy(INCOMING / "teams_proximus_offboarding_2026-09.json", database / "new.json")
            before = (database / "new.json").read_text()
            exit_code = main([
                "--database", str(database),
                "--all",
                "--apply",
                "--state", str(root / "state.json"),
                "--notif", str(root / "notif"),
                "--quiet",
            ])
            self.assertEqual(0, exit_code)
            self.assertEqual(before, (database / "new.json").read_text())
            self.assertFalse((root / "notif").exists())


if __name__ == "__main__":
    unittest.main()