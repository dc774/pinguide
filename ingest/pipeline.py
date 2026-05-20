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


def main() -> int:
    load_dotenv()

    Settings.embed_model = OpenAIEmbedding(model="text-embedding-3-small")
    Settings.llm = None

    print(f"Loading JSON files from {RAW_DIR}/...", flush=True)
    n_files, documents = load_documents(RAW_DIR)
    if not documents:
        print("No documents found. Run the scraper first.", file=sys.stderr)
        return 1
    print(f"Loaded {n_files} files → {len(documents)} documents", flush=True)

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)
    nodes = splitter.get_nodes_from_documents(documents)
    print(f"Split into {len(nodes)} nodes", flush=True)

    chroma_client = chromadb.PersistentClient(path=str(VECTOR_STORE_DIR))
    print("Clearing existing collection...", flush=True)
    try:
        chroma_client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = chroma_client.get_or_create_collection(COLLECTION_NAME)

    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    print(f"Embedding and storing {len(nodes)} nodes...", flush=True)
    VectorStoreIndex(nodes, storage_context=storage_context, show_progress=True)

    print(f"Done. {len(nodes)} nodes stored in {VECTOR_STORE_DIR}/", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
