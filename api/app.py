"""Flask API — single /query endpoint for the pinguide RAG system."""

from __future__ import annotations

import re
import chromadb
import openai
import voyageai
from dotenv import load_dotenv
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)
CORS(app)

_openai = openai.OpenAI()
_voyage = voyageai.Client() if os.getenv("VOYAGE_API_KEY") else None

_VECTOR_STORE = Path(__file__).parent.parent / "vector_store"
_chroma = chromadb.PersistentClient(path=str(_VECTOR_STORE))
_collection = _chroma.get_or_create_collection("rulesheets")

# Sorted longest-first so greedy matching picks the most specific name first
# (e.g. "Star Wars: Fall of the Empire" before "Star Wars").
_KNOWN_GAMES: list[str] = sorted(
    {m["game"] for m in _collection.get(include=["metadatas"])["metadatas"] if "game" in m},
    key=len,
    reverse=True,
)

_EMBED_MODEL = "text-embedding-3-small"
_CHAT_MODEL = "gpt-4o-mini"  # TEMPORARY — swap back to claude-haiku-4-5-20251001
_TOP_K = 10             # candidates fetched per game from vector store
_RERANK_TOP_N = 5       # top results kept after reranking (single game)
_RERANK_TOP_N_MULTI = 8 # top results kept when multiple game variants are in scope

_SYSTEM_PROMPT = (
    f"You are a pinball machine expert assistant. This system covers roughly "
    f"{len(_KNOWN_GAMES)} machines across Tiltforums, PAPA, and Bob's Guide rulesheet archives. "
    "All questions refer to pinball machines and their rules, modes, and strategies — not to "
    "bands, films, or other topics those names may refer to. Answer using ONLY the rulesheet "
    "excerpts provided below. "
    "If asked how many machines are covered or what machines are available, answer using the "
    "count above — do not say you don't know. "
    "If the question is just a game name with no specific question, provide a concise "
    "overview of that machine's main modes and strategy using the excerpts, and invite a "
    "follow-up question. "
    "If the excerpts cover more than one distinct machine, briefly note which machine each "
    "piece of advice applies to. If the excerpts don't contain enough information to answer "
    "confidently, say so — do not speculate."
)


def _embed(text: str) -> list[float]:
    resp = _openai.embeddings.create(model=_EMBED_MODEL, input=text)
    return resp.data[0].embedding


def _hyde_embed(question: str, games: list[str]) -> list[float]:
    """HyDE: embed a hypothetical rulesheet answer rather than the raw question.

    Pinball questions use everyday language; rulesheets use domain-specific
    terminology. Generating a hypothetical excerpt in rulesheet style before
    embedding dramatically narrows the vocabulary gap and improves retrieval.
    """
    game_hint = f" for {min(games, key=len)}" if games else ""
    prompt = (
        f"Write a short excerpt from a pinball machine rulesheet{game_hint} "
        f"that directly answers this question: {question}\n\n"
        "Use technical rulesheet language. 2-4 sentences. No preamble."
    )
    resp = _openai.chat.completions.create(
        model=_CHAT_MODEL,
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return _embed(resp.choices[0].message.content.strip())


_MFR_PREFIXES = (
    "Stern ", "Williams ", "Bally ", "Gottlieb ", "Data East ",
    "Sega ", "Jersey Jack ", "Chicago Gaming ", "Spooky ",
)

# Generic words that appear in game names but also mean the hobby itself —
# blocking them prevents false-positive game filters on general questions.
_MATCH_BLOCKLIST = frozenset({"pinball"})


def _short_name(game: str) -> str:
    """Strip a leading manufacturer prefix from a game name."""
    for pfx in _MFR_PREFIXES:
        if game.startswith(pfx):
            return game[len(pfx):]
    return game


def _extract_games(question: str) -> list[str]:
    """Return all game names relevant to the question.

    Finds the longest game name present in the query (by full name or by
    manufacturer-stripped short name), then expands to include any games
    whose short name shares the same prefix — e.g. "Trident" → "Trident 2022",
    "Godzilla" → "Stern Godzilla", "Metallica" → "Metallica Remastered".

    Uses word-boundary matching to avoid e.g. "Now" matching inside "know".
    """
    q = question.lower()

    def _word_in(name: str) -> bool:
        return bool(re.search(r'\b' + re.escape(name) + r'\b', q))

    matched = next(
        (g for g in _KNOWN_GAMES if _word_in(g.lower()) and g.lower() not in _MATCH_BLOCKLIST),
        None,
    )
    if matched is None:
        matched = next(
            (g for g in _KNOWN_GAMES
             if _word_in(_short_name(g).lower()) and _short_name(g).lower() not in _MATCH_BLOCKLIST),
            None,
        )
    if matched is None:
        return []
    short = _short_name(matched).lower()
    return [g for g in _KNOWN_GAMES if _short_name(g).lower().startswith(short)]


def _retrieve(embedding: list[float], games: list[str]) -> tuple[list[str], list[dict]]:
    if len(games) > 1:
        # Query each game variant separately so all are represented in the
        # candidate pool before reranking. A single $in query lets one game
        # monopolise all TOP_K slots by vector similarity alone.
        per_game_k = max(4, _TOP_K // len(games))
        all_docs: list[str] = []
        all_metas: list[dict] = []
        for game in games:
            results = _collection.query(
                query_embeddings=[embedding],
                n_results=per_game_k,
                where={"game": {"$eq": game}},
            )
            all_docs.extend(results["documents"][0])
            all_metas.extend(results["metadatas"][0])
        return all_docs, all_metas

    kwargs: dict = {"query_embeddings": [embedding], "n_results": _TOP_K}
    if len(games) == 1:
        kwargs["where"] = {"game": {"$eq": games[0]}}
    results = _collection.query(**kwargs)
    return results["documents"][0], results["metadatas"][0]


def _rerank(question: str, chunks: list[str], metadatas: list[dict], top_n: int) -> tuple[list[str], list[dict]]:
    """Rerank chunks with Voyage AI and return the top top_n results.

    Falls back to the original ordering if the API key is absent or the call fails.
    """
    if _voyage is None or not chunks:
        return chunks[:top_n], metadatas[:top_n]
    try:
        result = _voyage.rerank(
            query=question,
            documents=chunks,
            model="rerank-2",
            top_k=top_n,
        )
        idxs = [r.index for r in result.results]
        return [chunks[i] for i in idxs], [metadatas[i] for i in idxs]
    except Exception:
        return chunks[:top_n], metadatas[:top_n]


def _build_user_message(question: str, chunks: list[str], metadatas: list[dict]) -> str:
    parts: list[str] = []
    for chunk, meta in zip(chunks, metadatas):
        header = (
            f"[GAME: {meta['game']} | "
            f"MANUFACTURER: {meta['manufacturer']} | "
            f"SECTION: {meta['section_name']}]"
        )
        parts.append(f"{header}\n{chunk}")
    parts.append(f"Question: {question}")
    return "\n\n".join(parts)


_FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")


@app.get("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}


@app.get("/")
def index():
    return send_file(os.path.abspath(_FRONTEND))


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/stats")
def stats():
    metadatas = _collection.get(
        where={"section_name": {"$ne": "Machine Metadata"}},
        include=["metadatas"],
    )["metadatas"]
    machine_count = len({m["game"] for m in metadatas})
    return jsonify({"machines": machine_count})


@app.post("/query")
def query():
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    try:
        games = _extract_games(question)
        embedding = _hyde_embed(question, games)
        chunks, metadatas = _retrieve(embedding, games)
        top_n = _RERANK_TOP_N_MULTI if len(games) > 1 else _RERANK_TOP_N
        chunks, metadatas = _rerank(question, chunks, metadatas, top_n)
        user_message = _build_user_message(question, chunks, metadatas)

        message = _openai.chat.completions.create(
            model=_CHAT_MODEL,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        )
        answer = message.choices[0].message.content

        sources = [
            {
                "game": m["game"],
                "manufacturer": m["manufacturer"],
                "section_name": m["section_name"],
                "url": m.get("url", ""),
            }
            for m in metadatas
        ]

        return jsonify({"answer": answer, "sources": sources})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
