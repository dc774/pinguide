# Pinguide

A RAG (Retrieval-Augmented Generation) system for answering questions about pinball machines. Ask it anything — rules, modes, strategies, scoring — and it answers using real rulesheets and guides as its knowledge base rather than making things up.

Built as a personal project for pinball enthusiasts. Not commercial.

---

## How it works

1. **Scrape** — Fetches rulesheets from Tiltforums, PAPA, and Bob's Guide, plus machine metadata from the Open Pinball Database (OPDB).
2. **Ingest** — Chunks the scraped text by section, generates embeddings via OpenAI, and stores everything in a local ChromaDB vector store.
3. **Query** — When you ask a question, it embeds your query, retrieves the most relevant rulesheet sections, and passes them as context to Claude to generate a grounded answer.
4. **Serve** — A Flask API exposes a `/query` endpoint. A plain HTML frontend sends questions and displays answers with source attribution.

---

## Tech stack

| Layer | Tool |
|---|---|
| RAG framework | LlamaIndex |
| Vector store | ChromaDB (on disk) |
| Embeddings | OpenAI `text-embedding-3-small` |
| LLM | Anthropic Claude Haiku |
| API | Flask + flask-cors |
| Frontend | Plain HTML/CSS/JS |
| Deployment | Render.com |

---

## Local setup

### Prerequisites

- Python 3.11
- API keys for [Anthropic](https://console.anthropic.com), [OpenAI](https://platform.openai.com), and [OPDB](https://opdb.org) (all free tiers work)

### 1. Clone and create a virtual environment

```bash
git clone <repo-url>
cd pinguide-rag

python3.11 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Add your API keys

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

Open `.env` and add:

```
ANTHROPIC_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here
OPDB_API_KEY=your_key_here
```

### 4. Scrape the rulesheets (one-time)

This fetches rulesheets from Tiltforums and saves them to `data/raw/`. It takes a few minutes depending on how many machines are listed.

```bash
python -m scraper.tiltforums
```

You can also pull machine metadata from OPDB:

```bash
python -m scraper.opdb
```

### 5. Ingest into the vector store (one-time)

This chunks the scraped text, generates embeddings, and writes them to `vector_store/`. It costs a small amount in OpenAI embedding credits (roughly $0.50 for the full corpus).

```bash
python -m ingest.pipeline
```

### 6. Run the API

```bash
flask --app api/app run --port 5001
```

The API will be available at `http://localhost:5001`. Open `frontend/index.html` in your browser to use the UI.

---

## Project structure

```
pinguide-rag/
├── scraper/        # Scripts to fetch rulesheets and machine metadata
├── ingest/         # Chunking, embedding, and ChromaDB storage
├── api/            # Flask app — POST /query endpoint
├── frontend/       # Single-page HTML UI
├── data/raw/       # Scraped source files (gitignored)
└── vector_store/   # ChromaDB data (gitignored)
```

---

## Deployment

The app is deployed on [Render.com](https://render.com) as a Python web service.

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn api.app:app --timeout 120`
- **Environment variables:** set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPDB_API_KEY` in the Render dashboard
- **Disk:** mount at `/vector_store` to persist ChromaDB between deploys

`gunicorn.conf.py` in the project root sets `workers = 1` and `timeout = 120`. One worker keeps memory usage within Render's free 512 MB limit; the longer timeout prevents kills on slow cold-start queries.

Note: Render's free tier spins down after 15 minutes of inactivity, so the first query after a quiet period will be slow (~30 seconds). Fine for personal use.

---

## Notes

- The scrape and ingest steps only need to run once (or when you want fresh data). The Flask API only does retrieval and LLM calls at query time.
- Very new machine releases may not have a Tiltforums rulesheet yet.
- Physical layout questions ("where is the spinner?") are weaker — image support is planned for a future phase.
