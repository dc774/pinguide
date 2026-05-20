"""Flask API — single /query endpoint for the pinguide RAG system."""

from __future__ import annotations

import chromadb
import openai
from dotenv import load_dotenv
import os

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)
CORS(app)

_openai = openai.OpenAI()

_chroma = chromadb.PersistentClient(path="vector_store")
_collection = _chroma.get_collection("rulesheets")

# Sorted longest-first so greedy matching picks the most specific name first
# (e.g. "Star Wars: Fall of the Empire" before "Star Wars").
_KNOWN_GAMES: list[str] = sorted(
    {m["game"] for m in _collection.get(include=["metadatas"])["metadatas"] if "game" in m},
    key=len,
    reverse=True,
)

_EMBED_MODEL = "text-embedding-3-small"
_CHAT_MODEL = "gpt-4o-mini"  # TEMPORARY — swap back to claude-haiku-4-5-20251001
_TOP_K = 10

_SYSTEM_PROMPT = (
    "You are a pinball expert assistant. Answer the user's question using ONLY the "
    "rulesheet excerpts provided below. If the excerpts don't contain enough information "
    "to answer confidently, say so — do not speculate."
)


def _embed(text: str) -> list[float]:
    resp = _openai.embeddings.create(model=_EMBED_MODEL, input=text)
    return resp.data[0].embedding


def _extract_game(question: str) -> str | None:
    """Return the canonical game name if the question mentions a known game, else None."""
    q = question.lower()
    for game in _KNOWN_GAMES:  # longest-first — picks most specific match
        if game.lower() in q:
            return game
    return None


def _retrieve(embedding: list[float], game: str | None = None) -> tuple[list[str], list[dict]]:
    kwargs: dict = {"query_embeddings": [embedding], "n_results": _TOP_K}
    if game:
        kwargs["where"] = {"game": {"$eq": game}}
    results = _collection.query(**kwargs)
    return results["documents"][0], results["metadatas"][0]


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


@app.get("/")
def index():
    return send_file(os.path.abspath(_FRONTEND))


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/query")
def query():
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    try:
        embedding = _embed(question)
        game = _extract_game(question)
        chunks, metadatas = _retrieve(embedding, game)
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
            }
            for m in metadatas
        ]

        return jsonify({"answer": answer, "sources": sources})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
