"""CLI entry point.

Usage examples:
    python -m reasoner --database fixtures/corpus --all --show-graph
    python -m reasoner --database fixtures/corpus --database fixtures/incoming
    python -m reasoner --database database --since 2026-09-15 --json report.json
    python -m reasoner --database database --watch              # applies tag updates
    python -m reasoner --database database --watch --no-apply   # report only
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .apply import apply_updates
from .drift import AMBIGUITY_MARGIN, EPSILON, Evaluation, evaluate_document
from .graph import Cluster, TagGraph, build_clusters, build_tag_graph
from .loader import BatchItem, Document, load_documents, load_state, save_state, select_batch
from .watcher import watch

MIN_REFERENCE_DOCS = 4
BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_since(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return moment


def _mtime_iso(document: Document) -> str:
    return datetime.fromtimestamp(document.mtime_ns / 1e9, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _cluster_line(tag_graph: TagGraph, cluster: Cluster) -> str:
    parts = []
    for dimension_id, tags in cluster.dimension_tags.items():
        dimension = tag_graph.dimensions_by_id.get(dimension_id)
        label = dimension.label if dimension else dimension_id
        parts.append(f"{label}: {', '.join(tags)}")
    return f"{cluster.id} (n={len(cluster.members)}) [{' | '.join(parts)}]"


def _distance_table(evaluation: Evaluation, tag_graph: TagGraph, clusters_by_id: dict[str, Cluster]) -> str:
    lines = []
    for cluster_id, distance in evaluation.distances:
        cluster = clusters_by_id[cluster_id]
        lines.append(f"{cluster_id} d={distance:.2f} [{', '.join(cluster.label[:4])}]")
    return "  ".join(lines)


def _print_evaluation(evaluation: Evaluation, tag_graph: TagGraph, clusters_by_id: dict[str, Cluster], applied: bool = False) -> None:
    document = evaluation.document
    flag = "DRIFT" if evaluation.drifting else ("MIXED" if evaluation.ambiguous else "ok")
    print(f"[{flag}] {document.path}")
    print(f"  doc       {document.doc_id} | {document.source} | {document.sent_at} | {evaluation.status}")
    print(f"  tags      {' | '.join(document.display_tags)}")
    print(f"  nearest   {_cluster_line(tag_graph, evaluation.nearest.cluster)}  d={evaluation.nearest.distance:.2f}")
    print(f"  runner-up {_cluster_line(tag_graph, evaluation.runner_up.cluster)}  d={evaluation.runner_up.distance:.2f}")
    if evaluation.drifting_tag:
        dimension = evaluation.drifting_tag_dimension or "?"
        print(
            f'  drifting  tag "{evaluation.drifting_tag}" (dimension {dimension}) '
            f"pull {evaluation.drifting_pull:+.2f} vs {evaluation.nearest.cluster.id} "
            f"(best-fit relief {evaluation.drifting_relief:+.2f})"
        )
        if evaluation.removal_cluster:
            print(
                f"  without   {evaluation.removal_cluster} d={evaluation.removal_distance:.2f} "
                f"would become the best fit"
            )
    print("  conflicts")
    for match in evaluation.conflicting:
        offending = ", ".join(f"{tag} ({pull:+.2f})" for tag, pull in match.offending[:3])
        print(f"    - {match.cluster.id} d={match.distance:.2f} pull={match.pull:+.2f} | offending: {offending}")
    if evaluation.host_cluster:
        print(f'  host      "{evaluation.drifting_tag}" is native to {evaluation.host_cluster} ({evaluation.host_support:.0%})')
    for note in evaluation.notes:
        print(f"  note      {note}")
    for rank, suggestion in enumerate(evaluation.suggestions, start=1):
        print(
            f'  suggest   [{rank}] replace "{evaluation.drifting_tag}" with "{suggestion.tag}" '
            f"-> {suggestion.simulated_cluster} d={suggestion.simulated_distance:.2f} | {suggestion.reason}"
        )
    if applied:
        replacement = next((suggestion for suggestion in evaluation.suggestions if suggestion.fits), None)
        if replacement is not None:
            print(f'  applied   replaced "{evaluation.drifting_tag}" with "{replacement.tag}" (old tag logged to notif/)')
    print(f"  reason    {evaluation.reason}")
    print()


def _evaluation_to_dict(evaluation: Evaluation, tag_graph: TagGraph, applied: bool = False) -> dict:
    def match_to_dict(match, include_offending: bool = False) -> dict:
        payload = {
            "cluster": match.cluster.id,
            "label": list(match.cluster.label),
            "distance": round(match.distance, 4),
        }
        if include_offending:
            payload["pull"] = round(match.pull, 4)
            payload["offending_tags"] = [
                {"tag": tag, "dimension": tag_graph.dimension_of(tag), "pull": round(pull, 4)}
                for tag, pull in match.offending[:4]
            ]
        return payload

    document = evaluation.document
    return {
        "path": str(document.path),
        "doc_id": document.doc_id,
        "source": document.source,
        "type": document.doc_type,
        "sent_at": document.sent_at,
        "mtime": _mtime_iso(document),
        "status": evaluation.status,
        "excerpt": document.excerpt,
        "tags": list(document.display_tags),
        "canonical_tags": list(document.tags),
        "drifting": evaluation.drifting,
        "ambiguous": evaluation.ambiguous,
        "tag_conflict": evaluation.tag_conflict,
        "applied": applied,
        "nearest": match_to_dict(evaluation.nearest),
        "runner_up": match_to_dict(evaluation.runner_up),
        "distances": {cluster_id: round(distance, 4) for cluster_id, distance in evaluation.distances},
        "drifting_tag": evaluation.drifting_tag,
        "drifting_tag_dimension": evaluation.drifting_tag_dimension,
        "drifting_pull": round(evaluation.drifting_pull, 4),
        "drifting_relief": round(evaluation.drifting_relief, 4),
        "removal": {
            "cluster": evaluation.removal_cluster,
            "distance": round(evaluation.removal_distance, 4) if evaluation.removal_distance is not None else None,
        },
        "conflicting_clusters": [match_to_dict(match, include_offending=True) for match in evaluation.conflicting],
        "host_cluster": evaluation.host_cluster,
        "host_support": round(evaluation.host_support, 4),
        "suggested_tags": [
            {
                "tag": suggestion.tag,
                "score": round(suggestion.score, 4),
                "native_cluster": suggestion.native_cluster,
                "native_support": round(suggestion.native_support, 4),
                "co_occurrence": round(suggestion.co_occurrence, 4),
                "role_similarity": round(suggestion.role_similarity, 4),
                "lexical_similarity": round(suggestion.lexical_similarity, 4),
                "simulated_distance": round(suggestion.simulated_distance, 4),
                "simulated_cluster": suggestion.simulated_cluster,
                "fits": suggestion.fits,
                "reason": suggestion.reason,
            }
            for suggestion in evaluation.suggestions
        ],
        "reason": evaluation.reason,
        "notes": list(evaluation.notes),
    }


def _graph_to_dict(tag_graph: TagGraph, clusters: list[Cluster], bootstrap: bool) -> dict:
    return {
        "tags": len(tag_graph.vocab),
        "edges": tag_graph.graph.number_of_edges(),
        "reference_documents": tag_graph.n_reference_docs,
        "bootstrap": bootstrap,
        "dimensions": [
            {"id": dimension.id, "label": dimension.label, "tags": list(dimension.tags)}
            for dimension in tag_graph.dimensions
        ],
        "clusters": [
            {
                "id": cluster.id,
                "size": len(cluster.members),
                "label": list(cluster.label),
                "dimensions": {
                    dimension_id: list(tags)
                    for dimension_id, tags in cluster.dimension_tags.items()
                },
            }
            for cluster in clusters
        ],
    }


def _print_graph(tag_graph: TagGraph, clusters: list[Cluster], bootstrap: bool) -> None:
    print("Logical tag graph")
    print(f"  reference docs : {tag_graph.n_reference_docs}{' (bootstrap: reference = full corpus)' if bootstrap else ''}")
    print(f"  tags / edges   : {len(tag_graph.vocab)} / {tag_graph.graph.number_of_edges()}")
    print("  dimensions     :")
    for dimension in tag_graph.dimensions:
        print(f"    {dimension.id} [{dimension.label}] -> {', '.join(dimension.tags)}")
    print("  clusters       :")
    for cluster in clusters:
        print(f"    {_cluster_line(tag_graph, cluster)}")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reasoner",
        description="Re-evaluate client_tags of newly created or edited documents in corpus context.",
    )
    parser.add_argument("--database", action="append", type=Path, default=None,
                        help="directory with document JSON files (repeatable; default: database/)")
    parser.add_argument("--state", type=Path, default=BASE_DIR / ".state" / "processed.json",
                        help="processed-files checkpoint (default: .state/processed.json)")
    parser.add_argument("--epsilon", type=float, default=EPSILON,
                        help=f"fixed cosine-distance threshold (default: {EPSILON})")
    parser.add_argument("--all", action="store_true", help="evaluate every document, ignoring the checkpoint")
    parser.add_argument("--since", type=str, default=None,
                        help="evaluate files with mtime at or after this ISO timestamp")
    parser.add_argument("--json", type=Path, default=None, help="write the full report as JSON")
    parser.add_argument("--show-graph", action="store_true", help="print the logical graph")
    parser.add_argument("--k", type=int, default=None, help="override the number of document clusters")
    parser.add_argument("--dry-run", action="store_true", help="do not update the processed-files checkpoint")
    parser.add_argument("--quiet", action="store_true", help="suppress the text report")
    parser.add_argument("--fail-on-drift", action="store_true", help="exit 1 when any document drifts")
    parser.add_argument("--apply", dest="apply", action="store_true", default=None,
                        help="replace the odd tag with the top fitting suggestion and log the old tag")
    parser.add_argument("--no-apply", dest="apply", action="store_false",
                        help="report only, never write tag updates")
    parser.add_argument("--notif", type=Path, default=BASE_DIR / "notif",
                        help="file holding only the most recently replaced old tag (default: notif)")
    parser.add_argument("--watch", action="store_true",
                        help="keep running and re-evaluate on file changes; applies tag updates unless --no-apply")
    parser.add_argument("--interval", type=float, default=2.0, help="watcher poll interval in seconds (default: 2.0)")
    parser.add_argument("--debounce", type=float, default=1.0,
                        help="watcher quiet period after changes before a run (default: 1.0)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply is None:
        args.apply = args.watch  # watching applies tag updates by default; one-shot runs do not
    if args.watch:
        database_dirs = args.database or [BASE_DIR / "database"]
        return watch(database_dirs, lambda: run_once(args), interval=args.interval, debounce=args.debounce)
    return run_once(args)


def run_once(args: argparse.Namespace) -> int:
    database_dirs = args.database or [BASE_DIR / "database"]
    documents, errors = load_documents(database_dirs)
    if not documents:
        message = "no documents found in " + ", ".join(str(directory) for directory in database_dirs)
        print(message, file=sys.stderr)
        for error in errors:
            print(f"  error: {error}", file=sys.stderr)
        return 0

    since = _parse_since(args.since) if args.since else None
    if since is not None:
        batch = [
            BatchItem(document, "since")
            for document in documents
            if datetime.fromtimestamp(document.mtime_ns / 1e9, tz=timezone.utc) >= since
        ]
    else:
        batch = select_batch(documents, load_state(args.state), force_all=args.all)

    batch_paths = {item.document.path for item in batch}
    reference = [document for document in documents if document.path not in batch_paths]
    bootstrap = len(reference) < MIN_REFERENCE_DOCS
    if bootstrap:
        reference = documents

    extra_tags = [tag for item in batch for tag in item.document.tags]
    tag_graph = build_tag_graph(reference, extra_tags=extra_tags)
    clusters = build_clusters(reference, tag_graph, k=args.k)
    clusters_by_id = {cluster.id: cluster for cluster in clusters}

    evaluations = [
        evaluate_document(item.document, item.status, tag_graph, clusters, epsilon=args.epsilon, ambiguous_margin=AMBIGUITY_MARGIN)
        for item in batch
    ]

    applied_paths: list[str] = []
    if args.apply and bootstrap:
        # Bootstrap clusters include the evaluated documents themselves, so
        # their "drift" is not trustworthy. Never rewrite tags from them.
        print("bootstrap run: tag updates are not applied (reference corpus too small)", file=sys.stderr)
    elif args.apply and not args.dry_run:
        applied_paths, apply_errors = apply_updates(evaluations, args.notif)
        errors.extend(apply_errors)
    applied_set = set(applied_paths)

    if not args.quiet:
        if args.show_graph:
            _print_graph(tag_graph, clusters, bootstrap)
        print("=" * 78)
        print(
            f"corpus {len(documents)} docs | reference {len(reference)} | evaluated {len(evaluations)} "
            f"| epsilon {args.epsilon:.2f}"
        )
        print("=" * 78)
        if bootstrap:
            print("note: reference corpus is small, clusters were built from the full corpus (bootstrap run)")
        if not evaluations:
            print("no new or changed documents since the last run")
        for evaluation in evaluations:
            _print_evaluation(evaluation, tag_graph, clusters_by_id, applied=str(evaluation.document.path) in applied_set)
        drifting = sum(1 for evaluation in evaluations if evaluation.drifting)
        ambiguous = sum(1 for evaluation in evaluations if evaluation.ambiguous)
        print(f"summary: {len(evaluations)} evaluated | {drifting} drifting | {ambiguous} mixed | {len(evaluations) - drifting - ambiguous} clean")
        if not args.apply and any(evaluation.drifting or evaluation.tag_conflict for evaluation in evaluations):
            print("hint: rerun with --apply to replace the odd tag and log the old tag to notif/")
        for error in errors:
            print(f"error: {error}")

    if args.json:
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "epsilon": args.epsilon,
            "graph": _graph_to_dict(tag_graph, clusters, bootstrap),
            "counts": {
                "documents": len(documents),
                "reference": len(reference),
                "evaluated": len(evaluations),
                "drifting": sum(1 for evaluation in evaluations if evaluation.drifting),
                "ambiguous": sum(1 for evaluation in evaluations if evaluation.ambiguous),
            },
            "files": [
                _evaluation_to_dict(evaluation, tag_graph, applied=str(evaluation.document.path) in applied_set)
                for evaluation in evaluations
            ],
            "errors": errors,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2))

    if not args.dry_run:
        state_documents = documents
        if applied_paths:
            state_documents, reload_errors = load_documents(database_dirs)
            errors.extend(reload_errors)
        save_state(args.state, state_documents)

    if args.fail_on_drift and any(evaluation.drifting for evaluation in evaluations):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
