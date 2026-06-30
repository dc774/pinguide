"""Load scraped JSON files, chunk by section, embed, and store in ChromaDB."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from llama_index.core import Document, Settings, StorageContext, VectorStoreIndex
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore

RAW_DIR = Path("data/raw")
PAPA_DIR = Path("data/raw/papa")
BOBS_DIR = Path("data/raw/bobs")
STERN_DIR = Path("data/raw/stern")
CGC_DIR = Path("data/raw/cgc")
VECTOR_STORE_DIR = Path("vector_store")
COLLECTION_NAME = "rulesheets"

# Rulesheet sources define the canonical machine names. Manufacturer pages are
# supplementary: their game names (from page <h1> / slug) often differ from the
# rulesheet naming for the same machine (e.g. "Godzilla" vs "Stern Godzilla",
# "Kiss" vs "KISS"), so they are reconciled onto a canonical name before ingest.
RULESHEET_SOURCES = [(RAW_DIR, "tiltforums"), (PAPA_DIR, "papa"), (BOBS_DIR, "bobs")]
SUPPLEMENTARY_SOURCES = [(STERN_DIR, "stern"), (CGC_DIR, "cgc")]

_MFR_PREFIXES = (
    "stern ", "williams ", "bally ", "gottlieb ", "data east ",
    "sega ", "jersey jack ", "chicago gaming ", "spooky ",
)


def _canonical_key(name: str) -> str:
    """Normalize a game title so the same machine matches across sources.

    Lowercases, drops a subtitle after ':', drops a rulesheet-title tail
    ('... Rulesheet Version ...'), strips a trailing 'Pinball'/'rules' word and a
    leading manufacturer prefix. "Stern Godzilla", "Godzilla" and
    "Godzilla Pinball" all reduce to "godzilla".
    """
    s = name.lower().strip()
    s = s.split(":", 1)[0]
    s = re.split(r"\brulesheet\b", s, maxsplit=1)[0]
    s = s.strip()
    for suffix in (" pinball", " rules"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    for prefix in _MFR_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return re.sub(r"\s+", " ", s).strip()


def load_documents(raw_dir: Path) -> tuple[int, list[Document]]:
    files = sorted(raw_dir.glob("*.json"))
    documents: list[Document] = []

    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        for section_name, section_text in data.get("sections", {}).items():
            if not section_text or not section_text.strip():
                continue
            documents.append(
                Document(
                    text=section_text,
                    metadata={
                        "game": data["game"],
                        "manufacturer": data["manufacturer"],
                        "url": data["url"],
                        "section_name": section_name,
                    },
                )
            )

    return len(files), documents


def main() -> int:
    # override=True so .env wins over any stale key exported in the shell. See
    # the matching note in api/app.py. Safe in production (no .env is deployed).
    load_dotenv(override=True)

    Settings.embed_model = OpenAIEmbedding(model="text-embedding-3-small")
    Settings.llm = None

    all_documents: list[Document] = []
    total_files = 0
    print("Loading documents...", flush=True)

    # Pass 1: rulesheet sources establish the canonical machine names.
    for src_dir, label in RULESHEET_SOURCES:
        if not src_dir.exists():
            print(f"  {src_dir}/ not found — skipping", flush=True)
            continue
        n, docs = load_documents(src_dir)
        print(f"  {src_dir}/ → {n} files, {len(docs)} documents [{label}]", flush=True)
        total_files += n
        all_documents.extend(docs)

    # Map each canonical key to its rulesheet name. Skip keys claimed by more than
    # one distinct rulesheet name (ambiguous), so a supplementary page is only
    # merged when the target machine is unambiguous.
    key_to_names: dict[str, set[str]] = defaultdict(set)
    for doc in all_documents:
        key_to_names[_canonical_key(doc.metadata["game"])].add(doc.metadata["game"])
    canonical_by_key = {k: next(iter(v)) for k, v in key_to_names.items() if len(v) == 1}

    # Pass 2: supplementary (manufacturer) sources, reconciled onto canonical names.
    merges: dict[str, str] = {}
    for src_dir, label in SUPPLEMENTARY_SOURCES:
        if not src_dir.exists():
            print(f"  {src_dir}/ not found — skipping", flush=True)
            continue
        n, docs = load_documents(src_dir)
        for doc in docs:
            game = doc.metadata["game"]
            canon = canonical_by_key.get(_canonical_key(game))
            if canon and canon != game:
                doc.metadata["game"] = canon
                merges[game] = canon
        print(f"  {src_dir}/ → {n} files, {len(docs)} documents [{label}]", flush=True)
        total_files += n
        all_documents.extend(docs)

    if merges:
        print(f"Reconciled {len(merges)} manufacturer entries onto existing machines:", flush=True)
        for src, canon in sorted(merges.items()):
            print(f"    {src!r} → {canon!r}", flush=True)

    if not all_documents:
        print("No documents found. Run the scrapers first.", file=sys.stderr)
        return 1
    print(f"Total: {total_files} files → {len(all_documents)} documents", flush=True)

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)
    nodes = splitter.get_nodes_from_documents(all_documents)
    print(f"Split into {len(nodes)} nodes", flush=True)

    import shutil
    print("Clearing existing vector store...", flush=True)
    VECTOR_STORE_DIR.mkdir(parents=True, exist_ok=True)
    for item in VECTOR_STORE_DIR.iterdir():
        shutil.rmtree(item) if item.is_dir() else item.unlink()

    chroma_client = chromadb.PersistentClient(path=str(VECTOR_STORE_DIR))
    collection = chroma_client.get_or_create_collection(COLLECTION_NAME)

    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    print(f"Embedding and storing {len(nodes)} nodes...", flush=True)
    VectorStoreIndex(nodes, storage_context=storage_context, show_progress=True)

    print(f"Done. {len(nodes)} nodes stored in {VECTOR_STORE_DIR}/", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
