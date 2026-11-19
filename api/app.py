"""Flask API — single /query endpoint for the pinguide RAG system."""

from __future__ import annotations

import re
import anthropic
import chromadb
import openai
import voyageai
from dotenv import load_dotenv
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

# override=True makes .env authoritative for local dev, so a stale key exported
# in the shell (e.g. ANTHROPIC_API_KEY in ~/.zshrc) can't silently win. Safe in
# production: .env is gitignored and never deployed, so there is nothing to load
# and the platform-injected env vars (Railway dashboard) are left untouched.
load_dotenv(override=True)

app = Flask(__name__)
CORS(app)

_openai = openai.OpenAI()
_anthropic = anthropic.Anthropic()
_voyage = voyageai.Client() if os.getenv("VOYAGE_API_KEY") else None

_VECTOR_STORE = Path(__file__).parent.parent / "vector_store"
_chroma = chromadb.PersistentClient(path=str(_VECTOR_STORE))
_collection = _chroma.get_or_create_collection("rulesheets")

# Every game with a chunk in the store is a real rulesheet game.
# Sorted longest-first so greedy matching picks the most specific name first
# (e.g. "Star Wars: Fall of the Empire" before "Star Wars").
_all_metadatas = _collection.get(include=["metadatas"])["metadatas"]
_KNOWN_GAMES: list[str] = sorted(
    {m["game"] for m in _all_metadatas if "game" in m},
    key=len,
    reverse=True,
)

_EMBED_MODEL = "text-embedding-3-small"
_CHAT_MODEL = "claude-haiku-5-5"
# Haiku 5.5 thinks by default; "low" effort keeps Q&A fast and cheap, and lets
# the model skip thinking on simple requests. It also rejects temperature, so no
# sampling parameters are sent.
_EFFORT = "low"
_NO_ANSWER = "Sorry, I couldn't produce an answer to that. Please try rephrasing your question."
_TOP_K = 10             # candidates fetched per game from vector store
_RERANK_TOP_N = 5       # top results kept after reranking (single game)
_RERANK_TOP_N_MULTI = 8 # top results kept when multiple game variants are in scope
_MAX_HISTORY = 20       # prior chat messages sent to the LLM for context

_SYSTEM_PROMPT = (
    f"You are a pinball machine expert assistant. This system covers roughly "
    f"{len(_KNOWN_GAMES)} machines, drawing on community rulesheets, strategy guides, and "
    "official manufacturer pages. "
    "Questions are about pinball machines and their rules, modes, and strategies, not the "
    "bands, films, or other things those names may also refer to. "
    "Each excerpt below is labeled with its machine (GAME), manufacturer, and section, so "
    "attribute information to the correct machine. Some excerpts come from manufacturer "
    "marketing pages; prefer concrete gameplay, scoring, and mode detail over promotional language. "
    "Ground specific factual claims about a machine, exact scoring values, mode and wizard-mode "
    "names, multiball rules, layout, in the excerpts. For these specifics, if the excerpts don't "
    "contain the answer, say so rather than inventing details; do not guess scoring numbers or "
    "rule mechanics from general knowledge. "
    "For general pinball questions (how flippers or nudging work, history, terminology, how to "
    "improve), for conversational replies, and for machines not covered by the excerpts, you may "
    "use your own knowledge. When you answer from general knowledge rather than a provided "
    "rulesheet, make that clear (for example, note that it isn't from a sourced rulesheet) so the "
    "user knows it is not verified against this system's sources. "
    "This is a conversation: for a follow-up that does not name a machine, stay with the "
    "machine already under discussion. "
    "If asked how many machines are covered or which machines are available, answer using the "
    "count above; do not say you don't know. "
    "If the message is just a machine name with no specific question, give a concise overview "
    "of that machine's main modes and strategy from the excerpts, and invite a follow-up."
)


def _text(response) -> str:
    """Return the answer text from a Claude response.

    Reads blocks by type, not position: the response can start with thinking
    blocks, and a refusal (stop_reason "refusal") can carry no text at all, in
    which case this returns "".
    """
    return "".join(b.text for b in response.content if b.type == "text").strip()


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
    # max_tokens leaves room for thinking, which counts toward the cap; the
    # prompt itself keeps the excerpt short.
    resp = _anthropic.messages.create(
        model=_CHAT_MODEL,
        max_tokens=1024,
        output_config={"effort": _EFFORT},
        messages=[{"role": "user", "content": prompt}],
    )
    # Fall back to the raw question if no excerpt came back.
    return _embed(_text(resp) or question)


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


def _norm(text: str) -> str:
    """Lowercase, drop apostrophes, turn other punctuation into spaces, collapse whitespace."""
    text = re.sub(r"['‘’]", "", text.lower())
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def _extract_games(question: str) -> list[str]:
    """Return all game names relevant to the question.

    Finds the longest game name present in the query (by full name or by
    manufacturer-stripped short name), then expands to include any games
    whose short name shares the same prefix — e.g. "Trident" → "Trident 2022",
    "Godzilla" → "Stern Godzilla", "Metallica" → "Metallica Remastered".

    Uses word-boundary matching to avoid e.g. "Now" matching inside "know".
    Punctuation is ignored on both sides, so "Avengers Infinity Quest" matches
    "Avengers: Infinity Quest" instead of falling back to the 2012 "Avengers".
    """
    q = _norm(question)

    def _word_in(name: str) -> bool:
        name = _norm(name)
        return bool(name) and bool(re.search(r'\b' + re.escape(name) + r'\b', q))

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
    short = _norm(_short_name(matched))
    return [g for g in _KNOWN_GAMES if _norm(_short_name(g)).startswith(short)]


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


def _clean_history(raw: object) -> list[dict]:
    """Validate an incoming chat history into well-formed message dicts.

    Keeps only {"role": "user"|"assistant", "content": <non-empty str>} entries
    and drops anything malformed, so a bad client payload can't break the LLM call.
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content})
    return cleaned


def _games_in_context(question: str, history: list[dict]) -> list[str]:
    """Resolve the machine(s) in scope, carrying context forward on follow-ups.

    If the latest question names a machine, that wins. Otherwise scan the
    conversation backward and reuse the most recently discussed machine, so a
    nameless follow-up ("how does its wizard mode work?") stays scoped to it.
    """
    games = _extract_games(question)
    if games:
        return games
    for msg in reversed(history):
        if msg["role"] == "user":
            prior = _extract_games(msg["content"])
            if prior:
                return prior
    return []


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
    metadatas = _collection.get(include=["metadatas"])["metadatas"]
    machine_count = len({m["game"] for m in metadatas})
    return jsonify({"machines": machine_count})


@app.post("/query")
def query():
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    history = _clean_history(body.get("history"))

    try:
        games = _games_in_context(question, history)
        embedding = _hyde_embed(question, games)
        chunks, metadatas = _retrieve(embedding, games)
        top_n = _RERANK_TOP_N_MULTI if len(games) > 1 else _RERANK_TOP_N
        chunks, metadatas = _rerank(question, chunks, metadatas, top_n)

        recent = history[-_MAX_HISTORY:]
        if recent and recent[0]["role"] == "assistant":
            # The Anthropic API requires the first message to be from the user.
            recent = recent[1:]

        # Distinct machines that actually survived retrieval + reranking, ordered
        # shortest-name-first so an original precedes its variants (e.g. "Metallica"
        # before "Metallica Remastered").
        present_games = [
            g for g in sorted(games, key=len)
            if any(m["game"] == g for m in metadatas)
        ]

        if len(present_games) > 1:
            # Make one LLM call per machine and stitch the results under per-machine
            # headings. Doing the structure in code guarantees it regardless of model
            # variability; the recent turns still give each call conversational context.
            names = " and ".join(f"**{g}**" for g in present_games)
            answer_parts = [
                f"There are {len(present_games)} machines with this title ({names}), which "
                "can have different rules and modes. Here is each one, covered separately."
            ]
            for game in present_games:
                game_chunks = [c for c, m in zip(chunks, metadatas) if m["game"] == game]
                game_metas = [m for m in metadatas if m["game"] == game]
                user_msg = _build_user_message(question, game_chunks, game_metas)
                user_msg += (
                    f"\n\nAnswer ONLY about {game}. Do not begin with a bold title heading; "
                    f"the heading '**{game}**' is already shown above your answer. Start "
                    "directly with the content."
                )
                msg = _anthropic.messages.create(
                    model=_CHAT_MODEL,
                    max_tokens=2048,
                    output_config={"effort": _EFFORT},
                    system=_SYSTEM_PROMPT,
                    messages=recent + [{"role": "user", "content": user_msg}],
                )
                answer_parts.append(f"**{game}**\n\n{_text(msg) or _NO_ANSWER}")
            answer = "\n\n---\n\n".join(answer_parts)
        else:
            # Single conversational call: recent turns give follow-up context, the
            # latest question carries its retrieved excerpts.
            user_message = _build_user_message(question, chunks, metadatas)
            response = _anthropic.messages.create(
                model=_CHAT_MODEL,
                max_tokens=2048,
                output_config={"effort": _EFFORT},
                system=_SYSTEM_PROMPT,
                messages=recent + [{"role": "user", "content": user_message}],
            )
            answer = _text(response) or _NO_ANSWER

        sources = [
            {
                "game": m["game"],
                "manufacturer": m["manufacturer"],
                "section_name": m["section_name"],
                "url": m.get("url", ""),
            }
            for m in metadatas
        ]

        # Store the clean question (not the excerpt-laden message) so history
        # stays lean, and cap the total so the payload returned to (and re-sent
        # by) the client can't grow unbounded over a long session.
        updated_history = (history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ])[-_MAX_HISTORY:]
        return jsonify({"answer": answer, "sources": sources, "history": updated_history})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
