"""Load scraped JSON files, chunk by section, embed, and store in ChromaDB."""

from __future__ import annotations

import json
import sys
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
OPDB_DIR = Path("data/raw/opdb")
VECTOR_STORE_DIR = Path("vector_store")
COLLECTION_NAME = "rulesheets"


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


def load_opdb_documents(opdb_dir: Path) -> tuple[int, list[Document]]:
    files = sorted(opdb_dir.glob("*.json"))
    documents: list[Document] = []

    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        game = (data.get("game") or "").strip()
        if not game:
            continue

        manufacturer = data.get("manufacturer") or ""
        year = data.get("year")
        model_type = data.get("model_type") or ""

        parts = [f"Game: {game}."]
        if manufacturer:
            parts.append(f"Manufacturer: {manufacturer}.")
        if year:
            parts.append(f"Year: {year}.")
        if model_type:
            parts.append(f"Model type: {model_type}.")

        documents.append(
            Document(
                text=" ".join(parts),
                metadata={
                    "game": game,
                    "manufacturer": manufacturer,
                    "url": "",
                    "section_name": "Machine Metadata",
                },
            )
        )

    return len(files), documents


def main() -> int:
    load_dotenv()

    Settings.embed_model = OpenAIEmbedding(model="text-embedding-3-small")
    Settings.llm = None

    sources = [
        (RAW_DIR,  "tiltforums",    load_documents),
        (PAPA_DIR, "papa",          load_documents),
        (BOBS_DIR, "bobs",          load_documents),
        (OPDB_DIR, "opdb metadata", load_opdb_documents),
    ]

    all_documents: list[Document] = []
    total_files = 0
    print("Loading documents...", flush=True)
    for src_dir, label, loader in sources:
        if not src_dir.exists():
            print(f"  {src_dir}/ not found — skipping", flush=True)
            continue
        n, docs = loader(src_dir)
        print(f"  {src_dir}/ → {n} files, {len(docs)} documents [{label}]", flush=True)
        total_files += n
        all_documents.extend(docs)

    if not all_documents:
        print("No documents found. Run the scrapers first.", file=sys.stderr)
        return 1
    print(f"Total: {total_files} files → {len(all_documents)} documents", flush=True)

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)
    nodes = splitter.get_nodes_from_documents(all_documents)
    print(f"Split into {len(nodes)} nodes", flush=True)

    import shutil
    print("Clearing existing vector store...", flush=True)
    if VECTOR_STORE_DIR.exists():
        shutil.rmtree(VECTOR_STORE_DIR)
    VECTOR_STORE_DIR.mkdir(parents=True)

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
