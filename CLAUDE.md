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
| Language | Python 3.11 (pinned for Railway in `.python-version`) | Required by LlamaIndex; same version locally and in production |
| RAG framework | LlamaIndex | Handles chunking, embedding, retrieval |
| Vector store | ChromaDB | Runs in-process, persists to disk, no separate DB needed |
| Embeddings | OpenAI text-embedding-3-small | Cheap, high quality, ~$0.50 one-time for full corpus |
| LLM | Anthropic Claude (claude-haiku-5-5, effort "low") | Cheap per query, good Q&A quality |
| Reranking | Voyage AI rerank-2 | Reorders retrieved chunks by true relevance |
| API server | Flask + gunicorn | Simple, lightweight |
| Deployment | Railway (app + Volume for ChromaDB), Cloudflare DNS | Deploys from GitHub `main` |
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
│   ├── pinballrules.py     # pinballrulesheets.com (CC0 GitHub repo), primary source
│   ├── papa.py             # PAPA rulesheet archive — pre-2000 machines
│   ├── bobs_guide.py       # Bob's Guide — classic/EM era strategy
│   ├── stern.py            # Stern official game pages — new releases, edition differences
│   ├── cgc.py              # Chicago Gaming official pages: CGC remakes
│   ├── kineticist.py       # Kineticist API: profiles for ~1,780 games + tutorial transcripts
│   └── makers.py           # Smaller makers' sites (Multimorphic, Spooky, Turner, ...)
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
- **pinballrulesheets.com** (successor to the Tiltforums wiki rulesheets; Tiltforums closed
  2026-09-01). Source is the GitHub repo `heyrocker/pinballrules`, license CC0 (public domain).
  - `scraper/pinballrules.py` downloads the repo tarball once and reads `docs/<manufacturer>/*.md`
    in memory: front matter gives title/manufacturer, `## ` headings give sections. Skips
    `docs/contribute` and `docs/other` (reference lists, not machines). Rulesheets sharing an
    `opdb_id` are de-duplicated. It rewrites `data/raw/pinballrules/` fully on each run.
  - `data/raw/tiltforums/` is a frozen archive of the 6 old Tiltforums rulesheets the new repo
    doesn't carry (Doctor Who, Fathom Revisited, Junkyard, Trident 2022, World Cup Soccer,
    WrestleMania). There is no scraper for it any more.
  - Covers: Stern, Jersey Jack, Spooky, Chicago Gaming Company (Pulp Fiction), Williams, etc.
  - Format: Structured wiki pages with sections (Layout, Modes, Multiball, Wizard Modes)
  - How to get: Scrape via BeautifulSoup

### Legacy/Classic Machines
- **PAPA rulesheet archive** (`pinball.org`; host broken since late 2026, files kept)
  - Pre-2000 machines, text format, easy to scrape
- **Bob's Guide** — `https://rules.silverballmania.com/guide`
  - Classic/EM era strategy, clean single site, easy to scrape

### Manufacturer Official Pages (permitted; marketing-depth)
- **Stern Pinball:** `https://www.sternpinball.com/game/<slug>/`
  - robots.txt allows general crawling (blocks GPTBot/ChatGPT-User/Amazonbot/Scrapy, NOT ClaudeBot)
  - Discover game URLs from `https://www.sternpinball.com/sitemap.xml` (main /game/<slug>/ pages only)
  - Since Nov 2026 the site is Next.js/Plasmic: content is the CMS record embedded in the page's
    `__NEXT_DATA__` queryCache (description, playfield features, per-edition cabinet features).
    Older games have no CMS content; their June 2026 files are kept. Value: new releases,
    edition (Premium/LE) differences, physical-feature vocabulary. NOT deep rules.
- **Chicago Gaming (CGC)** — `https://www.chicago-gaming.com/coinop/<slug>`
  - robots.txt allows general crawling (disallows only /images)
  - Discover from the homepage nav (the XML sitemap omits Cactus Canyon and Pulp Fiction)
  - Covers the 5 CGC remakes.

> **Name reconciliation (important):** Manufacturer pages name games from the page `<h1>`/slug
> (e.g. "Godzilla", "Kiss"), which often differs from the rulesheet naming for the same machine
> ("Stern Godzilla", "KISS"). To avoid duplicate machine entries, `ingest/pipeline.py` loads the
> rulesheet sources FIRST to fix canonical names, then reconciles each manufacturer entry onto a
> canonical name via `_canonical_key()` (lowercase; fold accents; drop apostrophes, a trailing
> "Pinball"/"rules", a "...Rulesheet..." tail, and a leading manufacturer prefix). Subtitles after
> ':' are KEPT, because Stern reuses franchise names for different machines ("Avengers" 2012 vs
> "Avengers: Infinity Quest" 2020; same for Transformers and Avatar). If the full manufacturer
> title has no match, its subtitle alone is tried ("Star Wars: The Mandalorian" → "The
> Mandalorian"). A manufacturer page with no canonical match stays a standalone machine (the
> intended value: brand-new releases), and the sources after it can merge onto it (Kineticist's
> "Queen" joins Pinball Brothers' "Queen Pinball"). Kineticist profiles are matched EXACTLY first,
> by OPDB group ID or rulesheet link against `data/raw/pinballrules/` (`_exact_matches()`),
> because Kineticist repeats titles across eras; names are the fallback. `_canonical_key()` also
> ignores spaces, punctuation and a leading "The" ("Whitewater" = "White Water"), and rulesheet
> titles that differ only that way are unified into one machine under the most readable name
> (that step keeps the maker prefix, so "Stern Jurassic Park" stays apart from "Jurassic Park"). The reconciliation list
> is printed on every ingest: review it, especially the subtitle matches.

### Kineticist (game profiles for ~1,780 machines)
- Official API, free in early access: https://www.kineticist.com/docs/api. Catalog reads need no
  key (60/min, 2,000/day per IP). The website itself is behind a Vercel bot checkpoint; never
  scrape it, use only the API.
- **Terms:** personal projects permitted; any page showing the data must show visible
  attribution, "Data provided by Kineticist" linked to kineticist.com (the header credit in
  `frontend/index.html`; do not remove it). Don't build a competing database.
- `scraper/kineticist.py` saves per game: Profile, Design Team, Editions, and, when the game has
  a "Keys to the game" tutorial image, a transcript ("Getting Started (Kineticist tutorial)")
  made once by Claude Haiku 5.5 vision. Transcripts are cached in the JSON (`tutorials`), so
  `--force` refreshes the data without paying for images again. AVIF images are converted to
  PNG with Pillow. One full run is about 1,800 API calls and takes about an hour; if the daily
  cap is hit it stops cleanly, and a re-run resumes (existing files are skipped).
- Titles Kineticist uses more than once are saved as "Title (Maker, Year)".

### Smaller manufacturers (`scraper/makers.py`, `data/raw/<maker>/`)
- Multimorphic, Spooky, Pinball Brothers, Dutch Pinball, Turner, American Pinball, Barrels of
  Fun. robots.txt allowed all of them and none signals against AI (2026-11-20); each request
  is checked against robots.txt at run time. Excluded: Haggis and Quetzal (broken HTTPS
  certificates), Jersey Jack (its Shopify store answers scripts with 429 "local_rate_limited";
  do not work around it).
- Games come from each site's own listing pages, sitemap or feed. Spooky and Barrels of Fun
  have no clean "this is a game" signal, so their game names come from the Kineticist files
  (`kineticist_games()`): run `scraper.kineticist` first.
- Adds new releases (Dungeon Crawler Carl, Yukon Yeti, Queen, 26 Multimorphic P3 games, ...)
  and code update notes. Marketing depth, not full rules.

> **Note on AI-blocked sources:** Pinside, MatchPlay/Pintips, IPDB, GameFAQs and Pinball Primer
> all explicitly block AI crawlers in robots.txt (or the author objects) and must NOT be scraped.
> OPDB was removed (thin metadata only); Kineticist's richer profiles replace it.

---

## Environment Variables

```
# .env
ANTHROPIC_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here        # For embeddings only
VOYAGE_API_KEY=your_key_here        # For reranking (optional; falls back to vector order)
```

---

## How the RAG Pipeline Works

```
1. SCRAPE    → Fetch pinballrulesheets.com, PAPA, Bob's Guide, Stern, Chicago Gaming,
               the Kineticist API, and smaller manufacturers' sites
               Save raw text to data/raw/

2. INGEST    → Load raw text files
               Chunk by section (not fixed token windows — rulesheets have natural sections)
               Generate embeddings via OpenAI text-embedding-3-small
               Store chunks + embeddings in ChromaDB at vector_store/

3. QUERY     → User submits question (+ prior chat history) via frontend or API
               Resolve machine(s) in scope, carrying context forward on follow-ups
               Embed the question (HyDE), retrieve + rerank top-N chunks from Chroma
               One machine in scope: single Claude call (system + chunks + recent history)
               Multiple distinct machines: one Claude call per machine, stitched under
               per-machine headings in code (guarantees structure regardless of the model)
               Return answer + updated (capped) history

4. SERVE     → Flask app exposes POST /query endpoint
               Frontend (index.html) is a multi-turn chat: sends fetch() to /query with
               the running conversation, renders message bubbles, supports follow-ups
```

---

## Key Design Decisions

- **Chunk by section, not token count** — Rulesheets have natural section boundaries
  (modes, multiball, wizard modes). Chunking by section keeps related info together.
- **Scrape once, query many times** — The scrape + ingest is a one-time (or periodic) job.
  The Flask API only does retrieval + LLM calls at query time.
- **ChromaDB on disk** — Keeps it simple. No separate vector DB service to manage.
  On Railway, persists to a Railway Volume.
- **Claude Haiku 5.5 for queries** (`claude-haiku-5-5`). Cheapest Claude model, sufficient
  for Q&A. It thinks by default and rejects `temperature`, so calls send
  `output_config={"effort": "low"}` and no sampling parameters, and answer text is read
  by block type (`_text()` in `api/app.py`), never `content[0]`.
- **Pinned dependencies:** `requirements.txt` pins exact versions so Railway installs what
  was tested locally. In particular, `anthropic` 2.x switches to API version 2026-10-21;
  upgrade it deliberately and test locally first.
- **Name-match blocklist** (`_MATCH_BLOCKLIST` in `api/app.py`, approved by the owner):
  machine titles that are also everyday pinball words ("Jackpot", "Action", "Wizard!") are not
  auto-selected by name, so "What is a jackpot?" isn't narrowed to the 1970s game. The list is
  the titles appearing as ordinary words in at least 10% of other machines' rulesheets;
  recompute it if the corpus changes a lot.
- **OpenAI for embeddings only** — text-embedding-3-small is the best price/quality ratio
  for embeddings. This is the only reason OpenAI is in the stack.

---

## Code Standards

- **Accessibility (WCAG AA)** — All frontend HTML/CSS/JS must meet WCAG AA accessibility
  guidelines without needing to be asked. This includes:
  - Color contrast ratios: ≥ 4.5:1 for normal text, ≥ 3:1 for large text and non-text UI
  - Form labels: every input must have an associated `<label>` (visible or `.sr-only`)
  - Semantic HTML: use appropriate elements (`<button>`, `<nav>`, `<header>`, etc.)
  - Keyboard navigation: all interactive elements must be reachable and operable by keyboard

---

## Known Limitations (Phase 1)

- **No image support** — Physical layout questions ("where is X on the playfield") will
  be weak. Image/diagram support is planned for Phase 2.
- **Community rulesheets only cover machines that have been documented**: very new releases
  may not have a wiki rulesheet yet.
- **Most machines are profile-only:** about 1,100 machines come only from Kineticist
  (description, years, credits, editions, sometimes a tutorial). The system prompt tells the
  model to say when no detailed rulesheet is available rather than guess.
- **Code update notes retrieve poorly:** HyDE drafts a rulesheet-style passage, so a question
  like "what changed in the latest code update?" tends to pull rulesheet chunks instead of the
  dated "Code Update Notes, posted <date>: ..." sections.
- **Big franchises fan out:** "Star Wars" matches about 6 machines (each era's game is its own
  machine), and each gets its own Claude call, so those answers are long and slower.
- **Re-ingest causes brief downtime:** `ingest/pipeline.py` wipes the store before
  rebuilding, so production queries fail while it runs, and the app caches its machine list
  at startup, so the service must be restarted afterwards.

---

## Development Phases

- **Phase 1 (current)** — Text-only RAG. Scrape, ingest, query, deploy.
- **Phase 2 (deferred)** — Playfield layout support for location questions. Favored approach:
  generate per-machine "Playfield Layout" text offline from a top-down image via a vision-capable
  Claude model, ingest as an ordinary section (existing text retrieval then answers location
  questions). Blocker: no permitted source of top-down playfield images. IPDB/Pinside block AI,
  Wikimedia Commons has none usable, and search results grant no rights. Only viable via
  owner-supplied images for a curated set; deferred until those exist.

---

## Build Order

1. `scraper/*.py`: scrape each source (pinballrules, papa, bobs_guide, stern, cgc)
2. `ingest/pipeline.py` — chunk + embed + store scraped data
3. `api/app.py` — Flask query endpoint (multi-turn chat with history)
4. `frontend/index.html` — chat UI
5. Deploy to Railway

---

## Running Locally

```bash
# Activate venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run scraper (one-time)
python -m scraper.pinballrules

# Run ingest (one-time, after scraping)
python -m ingest.pipeline

# Run API
flask --app api/app run
```

---

## Deployment (Railway + Cloudflare)

- Railway Python web service, deployed from GitHub `dc774/pinguide`
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn api.app:app` (1 worker, 120s timeout in `gunicorn.conf.py`)
- Environment variables: set in the Railway dashboard (ANTHROPIC_API_KEY, OPENAI_API_KEY, VOYAGE_API_KEY)
- ChromaDB lives on a Railway Volume (not in git); `data/raw/` IS in git
- DNS: Cloudflare. `pinguide.info` is DNS-only to Railway; `www` is proxied

## Refreshing the Data (periodic)

1. `railway login` and `railway link` (once per machine; the login expires)
2. Scrape. Scrapers skip existing files unless `--force`:
   ```bash
   python -m scraper.pinballrules         # whole repo, edited daily; always a full refresh
   python -m scraper.kineticist --force   # ~1 hour; tutorial transcripts are reused
   python -m scraper.makers               # needs the Kineticist files; rewrites each maker
   python -m scraper.stern --force
   python -m scraper.cgc --force
   python -m scraper.papa                 # static archive: new entries only
   python -m scraper.bobs_guide           # static archive: new entries only
   ```
3. Review `git status` / `git diff --stat data/raw`
4. Review names for free: `python -m ingest.pipeline --dry-run` prints the rulesheet name
   unifications, the reconciliation list and the machine count without embedding. Then the
   optional local test: `python -m ingest.pipeline`, and run the API
5. Commit and push; Railway redeploys the code
6. Rebuild the production store (a deploy does NOT do this; the store is on the Volume).
   Open a shell in the service: Railway dashboard → pinguide service → Console tab (or
   `railway ssh`). It starts in `/app`. Plain `python` is not on the PATH there; run
   `.venv/bin/python -m ingest.pipeline` (about 10 minutes; live queries fail meanwhile).
   Then restart: Deployments tab → ⋮ on the active deployment → Restart (machine list is
   cached at startup)
7. Verify: `curl https://pinguide.info/stats` matches the local machine count
