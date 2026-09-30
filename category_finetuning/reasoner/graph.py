"""Logical, multi-dimensional, clustered relationship graph over client_tags.

Layer 1 - relationship graph
    Tags are nodes. Two tags are connected when they co-occur in reference
    documents. Edge weight is positive pointwise mutual information, so pairs
    that appear together more often than chance count more than merely popular
    tags.

Layer 2 - dimensions
    Louvain communities partition the tag graph into corpus dimensions
    (client/geo, process, compliance, ...). A document normally carries tags
    from several dimensions, which is what makes its profile multi-dimensional.
    Tags with no relationships land in one explicit "unlinked" dimension.

Layer 3 - clusters
    Reference documents are embedded as TF-IDF tag vectors and grouped with
    spherical k-means. Every cluster keeps its centroid, member documents,
    label tags and dimension breakdown so that drift can be attributed back to
    individual tags.

Everything here is in-memory reasoning state; nothing is written to disk.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Sequence

import networkx as nx
import numpy as np

from .loader import Document

UNLINKED_DIMENSION = "D0"


def _idf(doc_frequency: int, n_docs: int) -> float:
    return math.log((1.0 + n_docs) / (1.0 + doc_frequency)) + 1.0


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


@dataclass(frozen=True)
class Dimension:
    id: str
    tags: tuple[str, ...]
    label: str


@dataclass
class TagGraph:
    vocab: tuple[str, ...]
    tag_index: dict[str, int]
    idf: np.ndarray
    graph: nx.Graph
    dimensions: tuple[Dimension, ...]
    dimensions_by_id: dict[str, Dimension]
    tag_dimension: dict[str, str]
    doc_frequency: dict[str, int]
    pair_counts: Counter
    role_vectors: dict[str, np.ndarray]
    n_reference_docs: int

    def vectorize(self, tags: Iterable[str]) -> np.ndarray:
        """TF-IDF vector, L2-normalised. Unknown tags were pre-registered with
        the maximum idf, so a brand new tag is treated as maximally rare."""
        vector = np.zeros(len(self.vocab), dtype=float)
        for tag in set(tags):
            index = self.tag_index.get(tag)
            if index is not None:
                vector[index] = self.idf[index]
        return _unit(vector)

    def role_similarity(self, first: str, second: str) -> float:
        """How alike two tags behave in the corpus, from their PPMI context.
        Project names score high against other project names, topics against
        topics, which lets suggestions replace like with like."""
        first_vector = self.role_vectors.get(first)
        second_vector = self.role_vectors.get(second)
        if first_vector is None or second_vector is None:
            return 0.0
        return float(first_vector @ second_vector)

    def dimension_of(self, tag: str) -> str | None:
        return self.tag_dimension.get(tag)


@dataclass
class Cluster:
    id: str
    centroid: np.ndarray
    members: tuple[int, ...]
    label: tuple[str, ...]
    dimension_tags: dict[str, tuple[str, ...]]  # dimension id -> top tags
    tag_support: dict[str, float]               # tag -> share of members carrying it


def build_tag_graph(documents: Sequence[Document], extra_tags: Iterable[str] = ()) -> TagGraph:
    docs = list(documents)
    n_docs = len(docs)

    doc_frequency: Counter[str] = Counter()
    pair_counts: Counter[tuple[str, str]] = Counter()
    for doc in docs:
        tags = sorted(set(doc.tags))
        for tag in tags:
            doc_frequency[tag] += 1
        for i, first in enumerate(tags):
            for second in tags[i + 1:]:
                pair_counts[(first, second)] += 1

    vocab = tuple(sorted(set(doc_frequency) | set(extra_tags)))
    tag_index = {tag: index for index, tag in enumerate(vocab)}
    idf = np.array([_idf(doc_frequency.get(tag, 0), n_docs) for tag in vocab])

    graph = nx.Graph()
    graph.add_nodes_from(vocab)
    if n_docs:
        for (first, second), count in pair_counts.items():
            p_first = doc_frequency[first] / n_docs
            p_second = doc_frequency[second] / n_docs
            p_pair = count / n_docs
            weight = max(0.0, math.log2(p_pair / (p_first * p_second)))
            if weight > 0:
                graph.add_edge(first, second, weight=weight, count=count)

    dimensions, dimensions_by_id, tag_dimension = _detect_dimensions(graph, doc_frequency)

    # Tag role vectors: cosine over the set of tags each tag co-occurs with
    # (raw co-occurrence, before PPMI pruning). Two project names share their
    # topic neighbours even when they never appear together, so they score as
    # the same kind of tag; a project name and a topic share far fewer
    # neighbours, which is what lets suggestions replace like with like.
    role_vectors = _role_contexts(pair_counts, vocab, tag_index)

    return TagGraph(
        vocab=vocab,
        tag_index=tag_index,
        idf=idf,
        graph=graph,
        dimensions=dimensions,
        dimensions_by_id=dimensions_by_id,
        tag_dimension=tag_dimension,
        doc_frequency=dict(doc_frequency),
        pair_counts=pair_counts,
        role_vectors=role_vectors,
        n_reference_docs=n_docs,
    )


def _role_contexts(
    pair_counts: Counter,
    vocab: tuple[str, ...],
    tag_index: dict[str, int],
) -> dict[str, np.ndarray]:
    """Role vector per tag: cosine over its raw co-occurrence neighbourhood.

    Two project names share their topic neighbours even when they never appear
    together, so they end up similar without any hardcoded taxonomy.
    """
    size = len(vocab)
    contexts = np.zeros((size, size), dtype=float)
    for (first, second) in pair_counts:
        i, j = tag_index[first], tag_index[second]
        contexts[i, j] = 1.0
        contexts[j, i] = 1.0

    norms = np.linalg.norm(contexts, axis=1, keepdims=True)
    contexts = np.divide(contexts, norms, out=np.zeros_like(contexts), where=norms > 0)
    return {tag: contexts[tag_index[tag]] for tag in vocab}


def _detect_dimensions(graph: nx.Graph, doc_frequency: Counter) -> tuple[tuple[Dimension, ...], dict[str, Dimension], dict[str, str]]:
    unlinked = {tag for tag in graph.nodes if graph.degree(tag) == 0 and doc_frequency.get(tag, 0) > 0}
    linked = graph.subgraph([tag for tag in graph.nodes if tag not in unlinked and doc_frequency.get(tag, 0) > 0])

    communities: list[set[str]] = []
    if linked.number_of_edges():
        communities = [set(c) for c in nx.community.louvain_communities(linked, weight="weight", seed=7)]

    ordered = sorted(communities, key=lambda members: (-len(members), sorted(members)))
    dimensions: list[Dimension] = []
    tag_dimension: dict[str, str] = {}

    if unlinked:
        label = " / ".join(sorted(unlinked, key=lambda t: (-doc_frequency.get(t, 0), t))[:4])
        dimensions.append(Dimension(UNLINKED_DIMENSION, tuple(sorted(unlinked)), f"unlinked: {label}"))
        for tag in unlinked:
            tag_dimension[tag] = UNLINKED_DIMENSION

    next_index = 1
    for members in ordered:
        dimension_id = f"D{next_index}"
        next_index += 1
        tags = tuple(sorted(members))
        label = " / ".join(sorted(tags, key=lambda t: (-doc_frequency.get(t, 0), t))[:4])
        dimensions.append(Dimension(dimension_id, tags, label))
        for tag in tags:
            tag_dimension[tag] = dimension_id

    dimensions_by_id = {dimension.id: dimension for dimension in dimensions}
    return tuple(dimensions), dimensions_by_id, tag_dimension


def choose_k(n_reference_docs: int) -> int:
    if n_reference_docs <= 1:
        return 1
    # Small corpora: one cluster per few documents (fixtures: 14 docs -> 3).
    # Larger corpora: roughly one cluster per ten documents, so real databases
    # with many topics do not collapse into a few diluted clusters.
    heuristic = max(
        int(round(math.sqrt(n_reference_docs / 2))),
        int(round(n_reference_docs / 10)),
    )
    return max(2, min(heuristic, 25, n_reference_docs))


def build_clusters(
    reference_docs: Sequence[Document],
    tag_graph: TagGraph,
    k: int | None = None,
) -> list[Cluster]:
    docs = list(reference_docs)
    n_docs = len(docs)
    if n_docs == 0:
        return []

    matrix = np.vstack([tag_graph.vectorize(doc.tags) for doc in docs])
    if k is None:
        k = choose_k(n_docs)
    k = max(1, min(int(k), n_docs))

    if k == 1:
        labels = np.zeros(n_docs, dtype=int)
        centroids = np.array([_unit(matrix.mean(axis=0))])
    else:
        labels, centroids = _spherical_kmeans(matrix, k)

    clusters: list[Cluster] = []
    for index in range(len(centroids)):
        members = tuple(int(i) for i in np.where(labels == index)[0])
        if not members:
            continue
        centroid = _unit(centroids[index])
        label, dimension_tags = _label_cluster(centroid, tag_graph)
        support: Counter[str] = Counter()
        for member in members:
            for tag in set(docs[member].tags):
                support[tag] += 1
        clusters.append(
            Cluster(
                id=f"C{len(clusters) + 1}",
                centroid=centroid,
                members=members,
                label=label,
                dimension_tags=dimension_tags,
                tag_support={tag: count / len(members) for tag, count in support.items()},
            )
        )
    return clusters


def _label_cluster(centroid: np.ndarray, tag_graph: TagGraph, top: int = 6) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    order = np.argsort(centroid)[::-1]
    labels = [tag_graph.vocab[i] for i in order if centroid[i] > 1e-9][:top]
    dimension_tags: dict[str, tuple[str, ...]] = {}
    for tag in labels:
        dimension = tag_graph.tag_dimension.get(tag, "?")
        dimension_tags.setdefault(dimension, [])
        dimension_tags[dimension].append(tag)  # type: ignore[arg-type]
    return tuple(labels), {dim: tuple(tags) for dim, tags in dimension_tags.items()}


def _spherical_kmeans(matrix: np.ndarray, k: int, seed: int = 13, n_init: int = 10, max_iter: int = 100) -> tuple[np.ndarray, np.ndarray]:
    """Cosine k-means: rows are unit vectors, so assignment by dot product."""
    n_docs = matrix.shape[0]
    best_labels = np.zeros(n_docs, dtype=int)
    best_centroids = matrix[:k].copy()
    best_inertia = math.inf

    for init in range(n_init):
        rng = np.random.default_rng(seed + init)
        # k-means++ style seeding on cosine distance.
        first = int(rng.integers(n_docs))
        chosen = [first]
        distances = 1.0 - matrix @ matrix[first]
        while len(chosen) < k:
            probabilities = np.maximum(distances, 0.0)
            total = float(probabilities.sum())
            nxt = int(rng.integers(n_docs)) if total <= 0 else int(rng.choice(n_docs, p=probabilities / total))
            chosen.append(nxt)
            distances = np.minimum(distances, 1.0 - matrix @ matrix[nxt])

        centroids = matrix[chosen].copy()
        labels = np.full(n_docs, -1, dtype=int)
        for _ in range(max_iter):
            new_labels = (matrix @ centroids.T).argmax(axis=1)
            if np.array_equal(new_labels, labels):
                break
            labels = new_labels
            for index in range(k):
                members = labels == index
                if members.any():
                    centroids[index] = _unit(matrix[members].sum(axis=0))

        inertia = float((1.0 - (matrix * centroids[labels]).sum(axis=1)).mean())
        if inertia < best_inertia:
            best_inertia, best_labels, best_centroids = inertia, labels.copy(), centroids.copy()

    return best_labels, best_centroids
