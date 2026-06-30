"""Scrape Chicago Gaming Company pinball pages and save per-game JSON to data/raw/cgc/.

CGC's robots.txt permits general crawling (it disallows only /images). Pinball
titles live at /coinop/<slug> (the arcade/product/support pages are skipped).
Pages are marketing copy; the prose lives in <p> blocks alongside a spec table
that we filter out. CGC sells *remakes* (Medieval Madness, Attack from Mars,
Monster Bash, Cactus Canyon, Pulp Fiction); these are treated as distinct
entries from the Williams/Bally originals already in the corpus.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# The homepage nav links to every pinball title; the XML sitemap is incomplete
# (it omits Cactus Canyon and Pulp Fiction), so discover from the homepage.
HOME_URL = "https://www.chicago-gaming.com/"
USER_AGENT = "pinguide-rag scraper (personal RAG project)"

# Pinball game pages only: /coinop/<single-slug>. The trailing single-segment
# match naturally excludes deeper paths like /coinop/cactus-canyon/upgrade.
COINOP_URL_RE = re.compile(r"^https://www\.chicago-gaming\.com/coinop/[a-z0-9-]+$")

# Lowercased mid-title words when building a display name from a URL slug.
_SMALL_WORDS = {"of", "the", "a", "an", "and", "from", "in", "on", "for", "to"}

# A block with several of these is the spec/options table, not gameplay prose.
_SPEC_HINTS = ("Powder Coat", "Display Size", "Trim Color", "Model ",
               "Side Molding", "Lower Arch", "Front Molding")


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r


def slugify(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_").lower()
    return s or "unknown"


def title_from_slug(slug: str) -> str:
    """'attack-from-mars' -> 'Attack from Mars'."""
    words = slug.split("-")
    return " ".join(
        w if (i > 0 and w in _SMALL_WORDS) else w.capitalize()
        for i, w in enumerate(words)
    )


def fetch_urls() -> list[str]:
    soup = BeautifulSoup(_get(HOME_URL).text, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []
    for a in soup.find_all("a", href=True):
        url = urljoin(HOME_URL, a["href"])
        if COINOP_URL_RE.match(url) and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_sections(soup: BeautifulSoup) -> dict[str, str]:
    """Collect substantive marketing prose, skipping the spec table and dupes."""
    seen: set[str] = set()
    parts: list[str] = []
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) < 80:
            continue
        if sum(hint in text for hint in _SPEC_HINTS) >= 2:
            continue
        key = text[:200]
        if key in seen:
            continue
        seen.add(key)
        parts.append(text)
    return {"Description": "\n\n".join(parts)} if parts else {}


def fetch_game(url: str) -> dict:
    slug = url.rstrip("/").split("/")[-1]
    soup = BeautifulSoup(_get(url).text, "html.parser")
    return {
        "game": title_from_slug(slug),
        "manufacturer": "Chicago Gaming",
        "url": url,
        "sections": parse_sections(soup),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Chicago Gaming pinball pages.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N game pages")
    parser.add_argument("--out", type=Path, default=Path("data/raw/cgc"),
                        help="Output directory (default: data/raw/cgc)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between requests (default: 2.0)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing files")
    args = parser.parse_args()

    print(f"Discovering pinball titles from {HOME_URL}", flush=True)
    try:
        urls = fetch_urls()
    except Exception as e:
        print(f"ERROR fetching sitemap: {e}", file=sys.stderr)
        return 1
    print(f"Found {len(urls)} pinball URLs", flush=True)

    if args.limit is not None:
        urls = urls[: args.limit]
        print(f"Limiting to first {len(urls)}", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)

    saved = skipped = empty = errors = 0
    total = len(urls)

    for i, url in enumerate(urls, 1):
        prefix = f"[{i}/{total}]"
        try:
            data = fetch_game(url)
            game = data["game"]

            if not data["sections"]:
                print(f"{prefix} {game} — no content, skipped", flush=True)
                empty += 1
                continue

            out_path = args.out / f"{slugify(game)}.json"
            if out_path.exists() and not args.force:
                print(f"{prefix} {game} — skipped (exists)", flush=True)
                skipped += 1
                continue

            out_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"{prefix} {game} — saved", flush=True)
            saved += 1
        except Exception as e:
            print(f"{prefix} {url} — ERROR: {e}", file=sys.stderr, flush=True)
            errors += 1

        time.sleep(args.delay)

    print(f"Done. saved={saved} skipped={skipped} empty={empty} errors={errors}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
