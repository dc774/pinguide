# Pinguide

A RAG (Retrieval-Augmented Generation) system that answers questions about pinball machines using real rulesheets and strategy guides as its knowledge base. Ask anything about rules, modes, scoring, or strategy — it finds the relevant source material and generates a grounded answer.

**Live:** [pinguide.info](https://pinguide.info)

Built as a personal project. Not commercial.

---

## Table of contents

- [What it covers](#what-it-covers)
- [How it works](#how-it-works)
- [Retrieval strategy](#retrieval-strategy)
- [The system prompt](#the-system-prompt)
- [Tech stack](#tech-stack)
- [Costs](#costs)
- [Project structure](#project-structure)
- [Local setup](#local-setup)
- [Deployment](#deployment)
- [Planned](#planned)

---

## What it covers

| Source | What it is | Machines |
|---|---|---|
| [Pinball Rulesheets](https://pinballrulesheets.com) | Community rulesheets for modern machines (successor to the Tiltforums wikis; CC0 repo [heyrocker/pinballrules](https://github.com/heyrocker/pinballrules)) | ~91 |
| Tiltforums archive | 6 older Tiltforums rulesheets not carried over to the new site (Tiltforums closed in 2026) | 6 |
| [PAPA rulesheet archive](https://papa.org) | Classic machine rulesheets | ~141 |
| [Bob's Guide](https://rules.silverballmania.com) | Strategy guides for classic and EM-era machines | ~413 |
| [Stern Pinball](https://sternpinball.com) | Official pages for current Stern machines: overview, gameplay features, edition (Premium/LE) differences | ~81 |
| [Chicago Gaming](https://www.chicago-gaming.com) | Official pages for CGC remakes (Medieval Madness, Attack from Mars, Monster Bash, Cactus Canyon, Pulp Fiction) | 5 |
| [Kineticist](https://www.kineticist.com) ([API](https://www.kineticist.com/docs/api)) | Machine profiles: description, years, design team, editions with price and production run, and for 66 games a transcribed "keys to the game" tutorial. Data provided by Kineticist. | ~1,780 |
| Smaller makers | Official pages from Multimorphic (26 P3 games), Spooky, Pinball Brothers, Dutch Pinball, Turner, American Pinball, and Barrels of Fun code update notes | ~55 |

About **2,000 machines** and **18,800 indexed chunks** in total. Many machines (about 1,100) have only a Kineticist profile, not a full rulesheet, and answers say so.

---

## How it works

Three steps, run once to build the knowledge base:

1. **Scrape**: seven scrapers fetch rulesheets, manufacturer pages and Kineticist profiles and save structured JSON to `data/raw/`
2. **Ingest** — chunks each source by section, generates embeddings via OpenAI, and stores everything in a ChromaDB vector store (~380 MB on disk)
3. **Query** — at runtime, extracts the machine name from the question, retrieves the top candidates (per game variant, to guarantee equal representation), reranks them, and passes the best 5–8 to the LLM as context

We chunk by natural section boundaries (Modes, Multiball, Strategy, etc.) rather than fixed token windows, so each chunk is about one coherent topic.

---

## Retrieval strategy

Three techniques work together:

**Game name filtering** — when a question names a specific machine, retrieval is restricted to chunks from that machine only. The system handles manufacturer-prefixed titles ("Stern Godzilla" vs. "Godzilla") and expands to variants (e.g. "Trident" also retrieves "Trident 2022"). General questions with no machine name get global retrieval.

**HyDE (Hypothetical Document Embeddings)** — pinball questions use everyday language; rulesheets use domain-specific terminology. Before searching, the LLM writes a short hypothetical rulesheet excerpt that would answer the question, and we embed that instead of the raw question. This closes the vocabulary gap and significantly improves retrieval precision.

**Reranking** — after retrieval, Voyage AI's `rerank-2` cross-encoder scores each (question, chunk) pair for true relevance and reorders them. The top 5 go to the LLM for single-machine questions; when multiple variants of the same machine are in scope (e.g. Metallica and Metallica Remastered), each is queried separately and the top 8 go to the LLM, ensuring both versions are covered in the answer.

---

## The system prompt

The LLM's behavior is shaped by a system prompt, which continues to evolve as we learn what produces better answers:

```
You are a pinball machine expert assistant. This system covers roughly N machines
across community rulesheets, strategy guides, and manufacturer pages. All questions refer to
pinball machines and their rules, modes, and strategies — not to bands, films, or
other topics those names may refer to. Answer using ONLY the rulesheet excerpts
provided below.
If asked how many machines are covered or what machines are available, answer using
the count above — do not say you don't know.
If the question is just a game name with no specific question, provide a concise
overview of that machine's main modes and strategy using the excerpts, and invite a
follow-up question.
When excerpts from multiple distinct machines are present (e.g. an original and a
remastered version), you MUST structure your answer with a clearly labeled heading
for EACH machine and list each machine's information under its own heading. Never
blend information from different machines into a single undifferentiated list.
If the excerpts don't contain enough information to answer confidently, say so —
do not speculate.
```

The machine count (N) is injected at startup from the live vector store, so it stays accurate as new machines are added.

---

## Tech stack

| Layer | Tool |
|---|---|
| RAG framework | LlamaIndex |
| Vector store | ChromaDB (on disk) |
| Embeddings | OpenAI `text-embedding-3-small` |
| Reranking | Voyage AI `rerank-2` |
| LLM | Anthropic Claude Haiku 5.5 |
| API | Flask + flask-cors |
| Frontend | Plain HTML/CSS/JS |
| Deployment | Railway + Cloudflare |

---

## Costs

**One-time:** embedding the full corpus costs about $0.25 in OpenAI credits.

**Per query:**

| Step | Cost |
|---|---|
| HyDE generation | ~$0.0001 |
| Voyage reranking | ~$0.0002 |
| Answer generation | ~$0.001 |
| **Total** | **~$0.001–0.002** |

At 10 queries a day, monthly cost is well under $1.

---

## Project structure

```
pinguide-rag/
├── scraper/          # pinballrules.py, kineticist.py, makers.py, papa.py, bobs_guide.py, stern.py, cgc.py
├── ingest/           # pipeline.py — chunk, embed, store
├── api/              # app.py — Flask /query endpoint
├── frontend/         # index.html — single-page UI
├── data/raw/         # scraped JSON (committed to git)
└── vector_store/     # ChromaDB data (Railway Volume — not in git)
```

---

## Local setup

**Prerequisites:** Python 3.11, API keys for [Anthropic](https://console.anthropic.com), [OpenAI](https://platform.openai.com), and [Voyage AI](https://voyageai.com).

```bash
git clone <repo-url>
cd pinguide-rag
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys
```

Scraped data is already committed, so you can go straight to ingest:

```bash
python -m ingest.pipeline   # ~$0.25 in OpenAI embedding credits
flask --app api/app run --port 5001
```

To re-scrape fresh data first:

```bash
python -m scraper.pinballrules
python -m scraper.papa
python -m scraper.bobs_guide
python -m scraper.stern
python -m scraper.cgc
python -m scraper.kineticist   # about an hour; transcribes tutorial images once (ANTHROPIC_API_KEY)
python -m scraper.makers       # after kineticist (uses its game names)
```

`python -m ingest.pipeline --dry-run` prints the machine-name reconciliation and machine count without embedding anything.

---

## Deployment

Runs on [Railway](https://railway.com) as a Python web service. ChromaDB data lives on a 5 GB Railway Volume (not in git) and is **not** rebuilt by a deploy: after data changes, open the service's Console in the Railway dashboard, run `.venv/bin/python -m ingest.pipeline`, then restart the service.

- **Build:** `pip install -r requirements.txt`
- **Start:** `gunicorn api.app:app` (single worker and 120s timeout are set in `gunicorn.conf.py`)
- **Environment variables:** set all API keys in the Railway dashboard
- **Domain:** `pinguide.info` via Cloudflare DNS

---

## Planned

Playfield layout support for location questions ("where is the snake scoop in Metallica Remastered?"). The current text rulesheets describe elements by *function* (what the Snake does), not *position* (where it is), so these questions are weak today.

A feasibility review favors generating per-machine "Playfield Layout" text **offline** from a top-down playfield image via a vision-capable Claude model, then ingesting it as an ordinary section so the existing text retrieval answers location questions (no per-query vision cost, no frontend change). The technique is sound; the blocker is **sourcing permitted top-down playfield images**. The good sources (IPDB, Pinside) block AI use, Wikimedia Commons has no usable images for these machines, and a Google-style search confers no usage rights. The only viable path is owner-supplied images for a curated set of machines. **Deferred** until such images are available.
