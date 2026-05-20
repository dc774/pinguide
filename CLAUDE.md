# pinguide-rag — Architecture & Developer Guide

## What This Is

A RAG (Retrieval-Augmented Generation) system that answers pinball machine questions using
scraped rulesheets, guides, and machine metadata as its knowledge base. The system retrieves
relevant documents and passes them as context to Claude to generate grounded, accurate answers.

Personal project. Not commercial. Low traffic (owner + a few friends).

---

## Tech Stack

| Layer | Tool | Why |
|---|---|---|
| Language | Python 3.11 | Required by LlamaIndex |
| RAG framework | LlamaIndex | Handles chunking, embedding, retrieval |
| Vector store | ChromaDB | Runs in-process, persists to disk, no separate DB needed |
| Embeddings | OpenAI text-embedding-3-small | Cheap, high quality, ~$0.50 one-time for full corpus |
| LLM | Anthropic Claude (claude-haiku-4-5) | Cheap per query, good Q&A quality |
| API server | Flask | Simple, lightweight |
| Deployment | Render.com free tier | Zero config, deploys from GitHub |
| Frontend | Plain HTML/JS | Single page, no framework |

---

## Project Structure

```
pinguide-rag/
├── CLAUDE.md               # This file — read at the start of every session
├── README.md
├── requirements.txt        # All Python dependencies
├── .env.example            # Template for required env vars (committed)
├── .env                    # Actual secrets (never committed)
├── .gitignore
├── scraper/
│   ├── __init__.py
│   ├── tiltforums.py       # Scrape Tiltforums wiki rulesheets (primary source)
│   ├── opdb.py             # OPDB API client — machine metadata and model variants
│   ├── papa.py             # PAPA rulesheet archive — pre-2000 machines
│   └── bobs_guide.py       # Bob's Guide — classic/EM era strategy
├── ingest/
│   ├── __init__.py
│   └── pipeline.py         # Load scraped data → chunk → embed → store in Chroma
├── api/
│   ├── __init__.py
│   └── app.py              # Flask app — single /query POST endpoint
├── data/
│   └── raw/                # Raw scraped text/JSON files (gitignored if large)
├── vector_store/           # ChromaDB persisted data (gitignored)
└── frontend/
    └── index.html          # Simple query UI — text input, results display
```

---

## Data Sources

### Primary (modern machines)
- **Tiltforums wiki rulesheets** — `https://tiltforums.com/c/game-specific/rulesheet-wikis/18`
  - Master list: `https://tiltforums.com/t/rulesheet-master-list/7230`
  - Covers: Stern, Jersey Jack, Spooky, Chicago Gaming Company (Pulp Fiction), Williams, etc.
  - Format: Structured wiki pages with sections (Layout, Modes, Multiball, Wizard Modes)
  - How to get: Scrape via BeautifulSoup

### Machine Metadata
- **OPDB (Open Pinball Database)** — `https://opdb.org`
  - Free API, requires free account + token
  - Python client: `pip install open-pinball-db`
  - Covers: manufacturer, year, model variants (Pro/Premium/LE), OPDB IDs
  - Best for: answering "differences between Pro and Premium" questions

### Legacy/Classic Machines
- **PAPA rulesheet archive** — linked from Tiltforums master list
  - Pre-2000 machines, text format, easy to scrape
- **Bob's Guide** — `https://rules.silverballmania.com/guide`
  - Classic/EM era strategy, clean single site, easy to scrape

---

## Environment Variables

```
# .env
ANTHROPIC_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here        # For embeddings only
OPDB_API_KEY=your_key_here          # Free at opdb.org
```

---

## How the RAG Pipeline Works

```
1. SCRAPE    → Fetch rulesheet pages from Tiltforums, OPDB, PAPA, Bob's Guide
               Save raw text to data/raw/

2. INGEST    → Load raw text files
               Chunk by section (not fixed token windows — rulesheets have natural sections)
               Generate embeddings via OpenAI text-embedding-3-small
               Store chunks + embeddings in ChromaDB at vector_store/

3. QUERY     → User submits question via frontend or API
               Embed the question
               Retrieve top-N relevant chunks from Chroma
               Build prompt: system instructions + retrieved chunks + user question
               Send to Claude Haiku
               Return answer

4. SERVE     → Flask app exposes POST /query endpoint
               Frontend (index.html) sends fetch() to /query, displays response
```

---

## Key Design Decisions

- **Chunk by section, not token count** — Rulesheets have natural section boundaries
  (modes, multiball, wizard modes). Chunking by section keeps related info together.
- **Scrape once, query many times** — The scrape + ingest is a one-time (or periodic) job.
  The Flask API only does retrieval + LLM calls at query time.
- **ChromaDB on disk** — Keeps it simple. No separate vector DB service to manage.
  On Render, persists to the service's disk.
- **Claude Haiku for queries** — Cheapest Claude model, sufficient for Q&A tasks.
  Keeps per-query cost under $0.001.
- **OpenAI for embeddings only** — text-embedding-3-small is the best price/quality ratio
  for embeddings. This is the only reason OpenAI is in the stack.

---

## Known Limitations (Phase 1)

- **No image support** — Physical layout questions ("where is X on the playfield") will
  be weak. Image/diagram support is planned for Phase 2.
- **Tiltforums wikis only cover machines that have been documented** — Very new releases
  may not have a wiki rulesheet yet.
- **Render free tier sleeps** — After 15 minutes of inactivity, the service spins down.
  First query after sleep will be slow (~30s). Fine for personal use.

---

## Development Phases

- **Phase 1 (current)** — Text-only RAG. Scrape, ingest, query, deploy.
- **Phase 2** — Add playfield diagram images as context for layout questions.

---

## Build Order

1. `scraper/tiltforums.py` — scrape the master list, then each rulesheet page
2. `scraper/opdb.py` — pull machine metadata from OPDB API
3. `ingest/pipeline.py` — chunk + embed + store scraped data
4. `api/app.py` — Flask query endpoint
5. `frontend/index.html` — simple UI
6. Deploy to Render

---

## Running Locally

```bash
# Activate venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run scraper (one-time)
python -m scraper.tiltforums

# Run ingest (one-time, after scraping)
python -m ingest.pipeline

# Run API
flask --app api/app run
```

---

## Deployment (Render.com)

- Service type: Web Service
- Runtime: Python 3.11
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn api.app:app`
- Environment variables: set in Render dashboard (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPDB_API_KEY)
- Disk: mount at `/vector_store` to persist ChromaDB between deploys
