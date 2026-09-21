"""
RAG retriever.

VectorStore is a small interface so the backend is swappable per the
architecture doc (Chroma for local dev, pgvector-ready for production).
SimpleTfidfStore is the default here: a dependency-light, zero-network,
in-memory implementation using scikit-learn's TF-IDF + cosine similarity.
It's good enough at the synthetic-data scale used for the demo, and keeps
the whole pipeline runnable offline. Swap in a ChromaStore (same interface)
when embedding-model network access is available.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


@dataclass
class RetrievedDoc:
    doc_id: str
    doc_type: str  # "past_incident" | "runbook"
    text: str
    score: float


class VectorStore(Protocol):
    def add(self, doc_id: str, doc_type: str, text: str) -> None: ...
    def query(self, text: str, top_k: int = 3) -> list[RetrievedDoc]: ...


class SimpleTfidfStore:
    def __init__(self) -> None:
        self._ids: list[str] = []
        self._types: list[str] = []
        self._texts: list[str] = []
        self._vectorizer: TfidfVectorizer | None = None
        self._matrix = None

    def add(self, doc_id: str, doc_type: str, text: str) -> None:
        self._ids.append(doc_id)
        self._types.append(doc_type)
        self._texts.append(text)
        self._matrix = None  # invalidate cached matrix

    def _ensure_fitted(self) -> None:
        if self._matrix is not None:
            return
        if not self._texts:
            return
        self._vectorizer = TfidfVectorizer(stop_words="english")
        self._matrix = self._vectorizer.fit_transform(self._texts)

    def query(self, text: str, top_k: int = 3) -> list[RetrievedDoc]:
        self._ensure_fitted()
        if self._matrix is None or self._vectorizer is None:
            return []
        query_vec = self._vectorizer.transform([text])
        sims = cosine_similarity(query_vec, self._matrix)[0]
        ranked = sorted(
            zip(self._ids, self._types, self._texts, sims), key=lambda r: -r[3]
        )
        return [
            RetrievedDoc(doc_id=i, doc_type=t, text=txt, score=float(s))
            for i, t, txt, s in ranked[:top_k]
            if s > 0
        ]


def build_store_from_synthetic(
    synthetic_path: Path, runbooks_dir: Path | None = None
) -> SimpleTfidfStore:
    """Seed the store with synthetic past incidents + any runbook markdown files.
    Kept for backward compatibility — build_store() below is the same thing
    plus real incident history and is the preferred entry point going forward."""
    store = SimpleTfidfStore()

    if synthetic_path.exists():
        incidents = json.loads(synthetic_path.read_text())
        for inc in incidents:
            summary = (
                f"{inc['ground_truth_category']} on {inc['service']}: "
                + "; ".join(
                    str(e["payload"]) for e in inc["events"]
                )
            )
            store.add(inc["incident_id"], "past_incident", summary)

    if runbooks_dir and runbooks_dir.exists():
        for rb_path in runbooks_dir.glob("*.md"):
            store.add(rb_path.stem, "runbook", rb_path.read_text())

    return store


def build_store(
    synthetic_path: Path | None = None,
    runbooks_dir: Path | None = None,
    history_path: Path | None = None,
) -> SimpleTfidfStore:
    """
    Seeds a store from up to three sources: synthetic demo fixtures
    (optional — omit for a real deployment that shouldn't mix synthetic
    and real incidents in the same retrieval pool), runbook markdown
    files, and real incident history (src/rag/history.py) — the reports
    this same system has published and a human has approved in the past.

    This is what makes "similar past incidents" retrieval actually
    improve over time in production: every approved report becomes a
    retrievable document for the next incident, rather than the store
    starting empty on every real run.
    """
    store = SimpleTfidfStore()

    if synthetic_path and synthetic_path.exists():
        incidents = json.loads(synthetic_path.read_text())
        for inc in incidents:
            summary = (
                f"{inc['ground_truth_category']} on {inc['service']}: "
                + "; ".join(str(e["payload"]) for e in inc["events"])
            )
            store.add(inc["incident_id"], "past_incident", summary)

    if runbooks_dir and runbooks_dir.exists():
        for rb_path in runbooks_dir.glob("*.md"):
            store.add(rb_path.stem, "runbook", rb_path.read_text())

    if history_path:
        from src.rag.history import load_history
        for entry in load_history(history_path):
            # namespaced id so a real incident can never collide with a
            # synthetic fixture id if both happen to be seeded together
            store.add(f"history:{entry['incident_id']}", "past_incident", entry["summary_text"])

    return store


class ChromaStore:
    """
    Production swap-in for SimpleTfidfStore — same VectorStore interface,
    backed by a persistent Chroma collection with real embeddings instead
    of TF-IDF. Requires the `chroma` extra (`pip install -e ".[chroma]"`)
    and, if using Chroma's default embedding function, network access to
    download the embedding model on first use — unavailable in this
    sandbox, so this class is real/runnable but untested here. Verified
    only by tests/test_chroma_store.py, which is skipped automatically if
    chromadb isn't installed.

    Usage is a drop-in replacement:
        store = ChromaStore(persist_dir="./chroma_db")
    everywhere SimpleTfidfStore() is currently constructed.
    """

    def __init__(self, persist_dir: str = "./chroma_db", collection_name: str = "incidents"):
        try:
            import chromadb
        except ImportError as e:
            raise RuntimeError(
                "chromadb not installed. pip install -e '.[chroma]'"
            ) from e
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection(collection_name)

    def add(self, doc_id: str, doc_type: str, text: str) -> None:
        self._collection.upsert(
            ids=[doc_id], documents=[text], metadatas=[{"doc_type": doc_type}]
        )

    def query(self, text: str, top_k: int = 3) -> list[RetrievedDoc]:
        results = self._collection.query(query_texts=[text], n_results=top_k)
        docs = []
        ids = results.get("ids", [[]])[0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        for doc_id, doc_text, meta, dist in zip(ids, documents, metadatas, distances):
            # Chroma returns distance (lower = closer); convert to a similarity-like score
            score = 1.0 / (1.0 + dist)
            docs.append(
                RetrievedDoc(doc_id=doc_id, doc_type=meta.get("doc_type", "unknown"),
                             text=doc_text, score=score)
            )
        return docs
