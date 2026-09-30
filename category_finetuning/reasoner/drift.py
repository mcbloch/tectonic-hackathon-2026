"""Single-minded drift reasoning: distance vs epsilon + leave-one-tag-out.

For one evaluated document:
1. Embed its client_tags as a TF-IDF vector (same graph vocabulary as the
   reference corpus; unseen tags are maximum-idf so they cannot hide).
2. Measure cosine distance to every cluster centroid. The nearest distance is
   compared with the fixed epsilon.
3. Remove one tag at a time and re-measure. The "pull" of a tag for a cluster
   is how much the tag inflates the distance to that cluster. The tag with the
   largest pull away from the nearest cluster is the drifting tag.
4. Report the two clusters that drifting tag conflicts with most, where it
   would otherwise belong, and replacement candidates from reference
   co-occurrence.
5. Suggest concrete replacement tags from the cluster the document aligns
   with once the odd tag is removed, replacing like with like (project name
   for project name, topic for topic) and reporting the distance the document
   would reach after the swap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .graph import Cluster, TagGraph
from .loader import Document

# Fixed evaluation surface: cosine distance in [0, 1]. Calibrated once on the
# seed corpus so that coherent documents sit below it and mixed-context
# documents drift above it. Only override from the CLI for experiments.
EPSILON = 0.55

# A single tag counts as a conflict even when the whole document stays inside
# epsilon: this is the "one bad tag" signal.
TAG_CONFLICT_PULL = 0.10

# Two clusters whose distances differ by less than this are both plausible
# homes for the document.
AMBIGUITY_MARGIN = 0.08


@dataclass
class ClusterMatch:
    cluster: Cluster
    distance: float
    pull: float = 0.0
    offending: tuple[tuple[str, float], ...] = ()  # (tag, pull) sorted desc


@dataclass(frozen=True)
class TagSuggestion:
    tag: str
    score: float
    native_cluster: str      # cluster where the candidate tag is most common
    native_support: float    # share of that cluster's members carrying the tag
    co_occurrence: float     # co-occurrence with the remaining document tags
    role_similarity: float   # neighbourhood similarity with the replaced tag
    lexical_similarity: float  # token overlap with the replaced tag (label variants)
    simulated_distance: float  # best distance after the swap
    simulated_cluster: str
    fits: bool               # True when the swap brings the document under epsilon
    reason: str


def _tokenize(tag: str) -> frozenset[str]:
    return frozenset(token for token in re.split(r"[^a-z0-9]+", tag.lower()) if token)


def lexical_similarity(first: str, second: str) -> float:
    """Token overlap, so 'payroll cutoff' matches 'payroll_cutoff' or 'cutoff'.
    Catches label variants that an unseen tag would otherwise hide."""
    first_tokens, second_tokens = _tokenize(first), _tokenize(second)
    if not first_tokens or not second_tokens:
        return 0.0
    if first_tokens == second_tokens:
        return 1.0
    return len(first_tokens & second_tokens) / len(first_tokens | second_tokens)


@dataclass
class Evaluation:
    document: Document
    status: str
    distances: tuple[tuple[str, float], ...]  # (cluster id, distance), nearest first
    nearest: ClusterMatch
    runner_up: ClusterMatch
    drifting: bool
    ambiguous: bool
    tag_conflict: bool
    drifting_tag: str | None
    drifting_tag_dimension: str | None
    drifting_pull: float      # inflation of the nearest-cluster distance
    drifting_relief: float    # best-fit improvement when the tag is removed
    removal_distance: float | None
    removal_cluster: str | None
    conflicting: tuple[ClusterMatch, ...]
    host_cluster: str | None
    host_support: float
    suggestions: tuple[TagSuggestion, ...]
    reason: str
    notes: tuple[str, ...] = field(default_factory=tuple)


def _distance(vector, centroid) -> float:
    similarity = float(vector @ centroid)
    return max(0.0, min(1.0, 1.0 - similarity))


def evaluate_document(
    document: Document,
    status: str,
    tag_graph: TagGraph,
    clusters: Sequence[Cluster],
    epsilon: float = EPSILON,
    ambiguous_margin: float = AMBIGUITY_MARGIN,
) -> Evaluation:
    if not clusters:
        raise ValueError("cannot evaluate a document without at least one cluster")

    base = tag_graph.vectorize(document.tags)
    distances = [_distance(base, cluster.centroid) for cluster in clusters]

    # Leave-one-tag-out, computed once: pull[tag][cluster_index] is the amount
    # of distance that tag contributes to that cluster.
    without_distances: dict[str, list[float]] = {}
    pulls: dict[str, list[float]] = {}
    for tag in document.tags:
        without = tag_graph.vectorize([t for t in document.tags if t != tag])
        row = [_distance(without, cluster.centroid) for cluster in clusters]
        without_distances[tag] = row
        pulls[tag] = [distances[index] - row[index] for index in range(len(clusters))]

    def match(index: int, pulling_tag: str | None = None) -> ClusterMatch:
        offending = sorted(
            ((tag, pulls[tag][index]) for tag in document.tags),
            key=lambda item: item[1],
            reverse=True,
        )
        pull = pulls[pulling_tag][index] if pulling_tag is not None else 0.0
        return ClusterMatch(
            cluster=clusters[index],
            distance=distances[index],
            pull=pull,
            offending=tuple(offending),
        )

    order = sorted(range(len(clusters)), key=lambda index: distances[index])
    nearest_index = order[0]

    # Drifting tag: removal of this tag leaves the document with the closest
    # fit to any cluster. Ties break toward the rarer tag.
    drifting_tag = min(
        document.tags,
        key=lambda tag: (
            round(min(without_distances[tag]), 6),
            -float(tag_graph.idf[tag_graph.tag_index[tag]]),
        ),
    )
    drifting_pull = pulls[drifting_tag][nearest_index]
    removal_row = without_distances[drifting_tag]
    removal_index = min(range(len(clusters)), key=lambda index: removal_row[index])
    drifting_relief = distances[nearest_index] - removal_row[removal_index]

    # Clusters the drifting tag conflicts with most: largest positive pull
    # first, padded with the nearest clusters if fewer than two reject it.
    conflict_indices = [
        index
        for index in sorted(
            range(len(clusters)),
            key=lambda index: (-round(pulls[drifting_tag][index], 6), distances[index]),
        )
        if pulls[drifting_tag][index] > 1e-9
    ][:2]
    for index in order:
        if len(conflict_indices) >= 2:
            break
        if index not in conflict_indices:
            conflict_indices.append(index)
    conflicting = tuple(match(index, pulling_tag=drifting_tag) for index in conflict_indices)

    # Which cluster actually hosts the drifting tag in the reference corpus.
    host_cluster, host_support = None, 0.0
    for cluster in clusters:
        support = cluster.tag_support.get(drifting_tag, 0.0)
        if support > host_support:
            host_cluster, host_support = cluster.id, support

    nearest = match(nearest_index)
    runner_up = match(order[1]) if len(order) > 1 else nearest
    drifting = nearest.distance > epsilon
    ambiguous = (not drifting) and (runner_up.distance - nearest.distance) <= ambiguous_margin
    tag_conflict = (removal_index != nearest_index) or (drifting_pull >= TAG_CONFLICT_PULL)

    if drifting:
        reason = (
            f"distance {nearest.distance:.2f} to {nearest.cluster.id} exceeds epsilon {epsilon:.2f}; "
            f'removing "{drifting_tag}" improves the best fit to {removal_row[removal_index]:.2f} ({clusters[removal_index].id})'
        )
    elif ambiguous:
        reason = (
            f"within epsilon but {runner_up.cluster.id} is only "
            f"{runner_up.distance - nearest.distance:.2f} further away - context is mixed"
        )
    else:
        reason = f"distance {nearest.distance:.2f} to {nearest.cluster.id} within epsilon {epsilon:.2f}"

    notes: list[str] = []
    if removal_index != nearest_index:
        notes.append(f'single-tag conflict: removing "{drifting_tag}" moves the document to {clusters[removal_index].id}')
    elif drifting_pull >= TAG_CONFLICT_PULL:
        notes.append(f'single-tag conflict: "{drifting_tag}" pulls away from its nearest cluster by {drifting_pull:+.2f}')
    if host_cluster is not None and host_support > 0:
        notes.append(f'"{drifting_tag}" is native to {host_cluster} ({host_support:.0%} of its members)')
    if tag_graph.doc_frequency.get(drifting_tag, 0) == 0:
        notes.append(f'"{drifting_tag}" has no reference context (unseen tag) - suggestions come from the closest cluster')

    # A replacement is only worth proposing when a tag actually sticks out.
    suggestions: tuple[TagSuggestion, ...] = ()
    if drifting or ambiguous or tag_conflict:
        suggestions = suggest_tags(
            drifting_tag, document, tag_graph, clusters, removal_index, nearest.distance, epsilon
        )
        if suggestions and not any(suggestion.fits for suggestion in suggestions):
            notes.append("no replacement brings this document under epsilon - likely a new topic or label")

    return Evaluation(
        document=document,
        status=status,
        distances=tuple((clusters[index].id, distances[index]) for index in order),
        nearest=nearest,
        runner_up=runner_up,
        drifting=drifting,
        ambiguous=ambiguous,
        tag_conflict=tag_conflict,
        drifting_tag=drifting_tag,
        drifting_tag_dimension=tag_graph.dimension_of(drifting_tag),
        drifting_pull=drifting_pull,
        drifting_relief=drifting_relief,
        removal_distance=removal_row[removal_index],
        removal_cluster=clusters[removal_index].id,
        conflicting=conflicting,
        host_cluster=host_cluster,
        host_support=host_support,
        suggestions=suggestions,
        reason=reason,
        notes=tuple(notes),
    )


def suggest_tags(
    drifting_tag: str,
    document: Document,
    tag_graph: TagGraph,
    clusters: Sequence[Cluster],
    target_index: int,
    base_distance: float,
    epsilon: float,
    limit: int = 3,
) -> tuple[TagSuggestion, ...]:
    """Suggest tags from the target cluster that would fit the document better
    than the tag that sticks out.

    A candidate is scored by how much it improves the document's best fit, how
    native it is to the target cluster, and how closely its role (project name
    vs topic) matches the tag it replaces. Tags that are companions of the
    replaced tag are penalised: they are not substitutes.
    """
    remaining = [tag for tag in document.tags if tag != drifting_tag]
    target_cluster = clusters[target_index]
    pool = {
        tag: support
        for tag, support in target_cluster.tag_support.items()
        if tag not in document.tags
    }
    # Label variants anywhere in the vocabulary are candidates too: a tag new
    # to the corpus often has an existing spelling ("payroll cutoff" vs
    # "payroll_cutoff"), which is the most useful replacement to suggest.
    lexical: dict[str, float] = {}
    for tag in tag_graph.vocab:
        if tag in document.tags or tag_graph.doc_frequency.get(tag, 0) == 0:
            continue
        similarity = lexical_similarity(tag, drifting_tag)
        lexical[tag] = similarity
        if similarity >= 0.34:
            pool.setdefault(tag, 0.0)
    if not pool:
        pool = dict(lexical)

    suggestions: list[TagSuggestion] = []
    for candidate, support in pool.items():
        frequency = tag_graph.doc_frequency.get(candidate, 0)
        if frequency == 0:
            continue
        co_occurrence = sum(
            tag_graph.pair_counts.get(tuple(sorted((candidate, tag))), 0) for tag in remaining
        ) / (frequency * max(len(remaining), 1))
        role = tag_graph.role_similarity(candidate, drifting_tag)
        lexical_score = lexical.get(candidate, 0.0)
        # Where the candidate actually lives (support in the target cluster is
        # zero for label variants found outside it).
        native_cluster, native_support = target_cluster.id, support
        for cluster in clusters:
            cluster_support = cluster.tag_support.get(candidate, 0.0)
            if cluster_support > native_support:
                native_support, native_cluster = cluster_support, cluster.id
        # How often the candidate and the replaced tag appear together: a
        # companion is not a substitute.
        drifting_frequency = tag_graph.doc_frequency.get(drifting_tag, 0) or 1
        overlap = tag_graph.pair_counts.get(tuple(sorted((candidate, drifting_tag))), 0) / min(frequency, drifting_frequency)

        swapped = tag_graph.vectorize([*remaining, candidate])
        swapped_distances = [_distance(swapped, cluster.centroid) for cluster in clusters]
        simulated_index = min(range(len(clusters)), key=lambda index: swapped_distances[index])
        simulated = swapped_distances[simulated_index]
        gain = base_distance - simulated

        score = (
            0.45 * gain + 0.20 * native_support + 0.15 * role + 0.20 * lexical_score
        ) * (1.0 - 0.6 * min(overlap, 1.0))
        fits = simulated <= epsilon

        reasons = [f'native to {native_cluster} ({native_support:.0%} of members)']
        if lexical_score >= 0.5:
            reasons.append(f'label variant of "{drifting_tag}" (token overlap {lexical_score:.2f})')
        if role >= 0.5:
            reasons.append(f'same role as "{drifting_tag}" (similarity {role:.2f})')
        elif role >= 0.05:
            reasons.append(f'role similarity with "{drifting_tag}": {role:.2f}')
        if gain > 0.01:
            reasons.append(
                f'replacing it fits {clusters[simulated_index].id} at d={simulated:.2f} '
                f'(gain {gain:+.2f})'
            )
        if not fits:
            reasons.append(f"still above epsilon ({simulated:.2f}) - no close replacement")
        suggestions.append(
            TagSuggestion(
                tag=candidate,
                score=score,
                native_cluster=native_cluster,
                native_support=native_support,
                co_occurrence=co_occurrence,
                role_similarity=role,
                lexical_similarity=lexical_score,
                simulated_distance=simulated,
                simulated_cluster=clusters[simulated_index].id,
                fits=fits,
                reason="; ".join(reasons),
            )
        )

    suggestions.sort(key=lambda item: (not item.fits, -item.score, item.simulated_distance, item.tag))
    return tuple(suggestions[:limit])
