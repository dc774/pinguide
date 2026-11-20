"""Load scraped JSON files, chunk by section, embed, and store in ChromaDB."""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from llama_index.core import Document, Settings, StorageContext, VectorStoreIndex
from llama_index.core.node_parser import SentenceSplitter
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore

from scraper.makers import MAKERS

RAW_DIR = Path("data/raw")
PINBALLRULES_DIR = RAW_DIR / "pinballrules"
# Tiltforums closed in Sept 2026. The few machines not carried over to
# pinballrulesheets.com are kept here as a frozen archive.
TILTFORUMS_DIR = RAW_DIR / "tiltforums"
PAPA_DIR = RAW_DIR / "papa"
BOBS_DIR = RAW_DIR / "bobs"
STERN_DIR = RAW_DIR / "stern"
CGC_DIR = RAW_DIR / "cgc"
KINETICIST_DIR = RAW_DIR / "kineticist"
VECTOR_STORE_DIR = Path("vector_store")
# The app's SQLite database lives in vector_store/app/ (see api/db.py).
APP_DATA_DIRNAME = "app"
COLLECTION_NAME = "rulesheets"

# Rulesheet sources define the canonical machine names. Manufacturer pages and
# Kineticist profiles are supplementary: their game names often differ from the
# rulesheet naming for the same machine (e.g. "Godzilla" vs "Stern Godzilla",
# "Kiss" vs "KISS"), so they are reconciled onto a canonical name before ingest.
# Order matters: a supplementary machine with no match becomes a target for the
# sources after it (e.g. Kineticist's "Queen" joins Pinball Brothers' "Queen").
RULESHEET_SOURCES = [
    (PINBALLRULES_DIR, "pinballrules"), (TILTFORUMS_DIR, "tiltforums"),
    (PAPA_DIR, "papa"), (BOBS_DIR, "bobs"),
]
SUPPLEMENTARY_SOURCES = [
    (STERN_DIR, "stern"), (CGC_DIR, "cgc"),
    *[(RAW_DIR / maker, maker) for maker in MAKERS],
    (KINETICIST_DIR, "kineticist"),
]

_MFR_PREFIXES = (
    "stern ", "williams ", "bally ", "gottlieb ", "data east ",
    "sega ", "jersey jack ", "chicago gaming ", "spooky ",
    "american pinball ", "pinball brothers ", "dutch pinball ", "turner pinball ",
    "barrels of fun ", "multimorphic ",
)


def _canonical_key(name: str, strip_maker: bool = True) -> str:
    """Normalize a game title so the same machine matches across sources.

    Lowercases, drops a rulesheet-title tail ('... Rulesheet Version ...'),
    strips a trailing 'Pinball'/'rules' word and a leading manufacturer prefix.
    "Stern Godzilla", "Godzilla" and "Godzilla Pinball" all reduce to "godzilla".
    A subtitle after ':' is kept, because Stern reuses franchise names for
    different machines ("Avengers" 2012 vs "Avengers: Infinity Quest" 2020).
    Finally a leading "the", spaces and punctuation are dropped, so PAPA's
    run-together titles match: "Whitewater" = "White Water",
    "Addamsfamily" = "The Addams Family".
    """
    # Fold accents ("Pokémon" -> "pokemon") and drop apostrophes, straight or
    # curly ("Batman '66" -> "batman 66", "Elvira’s" -> "elviras").
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"['‘’]", "", s.lower()).strip()
    s = re.split(r"\brulesheet\b", s, maxsplit=1)[0]
    s = s.strip()
    for suffix in (" pinball", " rules"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    for prefix in _MFR_PREFIXES if strip_maker else ():
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = re.sub(r"^the\s+", "", s.strip())
    return re.sub(r"[^a-z0-9]", "", s)


def _exact_matches() -> dict[str, str]:
    """Kineticist profile URL -> rulesheet machine name, by OPDB ID or rulesheet link.

    Exact IDs beat name matching: Kineticist repeats titles across eras
    ("Godzilla" by Sega and by Stern), which names alone can't tell apart.
    """
    by_opdb: dict[str, str] = {}
    by_link: dict[str, str] = {}
    for path in PINBALLRULES_DIR.glob("*.json"):
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("opdb_group"):
            by_opdb[d["opdb_group"]] = d["game"]
        by_link[d["url"]] = d["game"]
    matches: dict[str, str] = {}
    for path in KINETICIST_DIR.glob("*.json"):
        d = json.loads(path.read_text(encoding="utf-8"))
        rules = re.sub(r"\.html$", "", d.get("rules_url") or "")
        canon = by_opdb.get(d.get("opdb_group") or "") or by_link.get(rules)
        if canon:
            matches[d["url"]] = canon
    return matches


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


def main(dry_run: bool = False) -> int:
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
            print(f"  {src_dir}/ not found, skipping", flush=True)
            continue
        n, docs = load_documents(src_dir)
        print(f"  {src_dir}/ → {n} files, {len(docs)} documents [{label}]", flush=True)
        total_files += n
        all_documents.extend(docs)

    # Rulesheet sources sometimes name one machine differently (PAPA's
    # "Addamsfamily", Bob's "The Addams Family"). Unify them under the most
    # readable name (most words, then longest). The key here keeps the maker
    # name: "Stern Jurassic Park" (2019) is not Data East's "Jurassic Park".
    same_title: dict[str, set[str]] = defaultdict(set)
    for doc in all_documents:
        same_title[_canonical_key(doc.metadata["game"], strip_maker=False)].add(doc.metadata["game"])
    renames: dict[str, str] = {}
    for key, names in same_title.items():
        if key and len(names) > 1:
            best = max(names, key=lambda n: (n.count(" "), len(n)))
            renames.update({n: best for n in names if n != best})
    for doc in all_documents:
        doc.metadata["game"] = renames.get(doc.metadata["game"], doc.metadata["game"])
    if renames:
        print(f"Unified {len(renames)} rulesheet names:", flush=True)
        for src, best in sorted(renames.items()):
            print(f"    {src!r} → {best!r}", flush=True)
    # Map each canonical key to its rulesheet name. Skip keys still claimed by
    # more than one name (e.g. "Jurassic Park" and "Stern Jurassic Park"), so a
    # supplementary page is only merged when the target machine is unambiguous.
    key_to_names: dict[str, set[str]] = defaultdict(set)
    for doc in all_documents:
        key_to_names[_canonical_key(doc.metadata["game"])].add(doc.metadata["game"])
    # An empty key (e.g. a title with no Latin characters) must never match.
    canonical_by_key = {k: next(iter(v)) for k, v in key_to_names.items() if k and len(v) == 1}

    # Pass 2: supplementary sources, reconciled onto canonical names: exact IDs
    # first, then names.
    exact = {url: renames.get(name, name) for url, name in _exact_matches().items()}
    merges: dict[str, str] = {}
    for src_dir, label in SUPPLEMENTARY_SOURCES:
        if not src_dir.exists():
            print(f"  {src_dir}/ not found, skipping", flush=True)
            continue
        n, docs = load_documents(src_dir)
        for doc in docs:
            game = doc.metadata["game"]
            key = _canonical_key(game)
            canon = exact.get(doc.metadata["url"]) or canonical_by_key.get(key)
            if canon is None and ":" in game:
                # Franchise-prefixed titles: "Star Wars: The Mandalorian" matches
                # the rulesheet "The Mandalorian" by its subtitle.
                canon = canonical_by_key.get(_canonical_key(game.split(":", 1)[1]))
            if canon and canon != game:
                doc.metadata["game"] = canon
                merges[game] = canon
            elif canon is None and key and key not in key_to_names:
                # A new machine: later sources can merge onto it.
                key_to_names[key].add(game)
                canonical_by_key[key] = game
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
    machines = len({d.metadata["game"] for d in all_documents})
    print(f"Machines: {machines}", flush=True)
    if dry_run:
        print("Dry run: nothing embedded or stored.", flush=True)
        return 0

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=50)
    nodes = splitter.get_nodes_from_documents(all_documents)
    print(f"Split into {len(nodes)} nodes", flush=True)

    import shutil
    print("Clearing existing vector store...", flush=True)
    VECTOR_STORE_DIR.mkdir(parents=True, exist_ok=True)
    for item in VECTOR_STORE_DIR.iterdir():
        if item.name == APP_DATA_DIRNAME:
            continue  # accounts and saved chats (api/db.py) share the Volume: never wipe them
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
    sys.exit(main(dry_run="--dry-run" in sys.argv[1:]))
