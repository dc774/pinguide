"""Scrape Stern Pinball game pages and save per-game JSON to data/raw/stern/.

Stern's robots.txt permits general crawling (it blocks only GPTBot/ChatGPT-User/
Amazonbot/Scrapy, not ClaudeBot or default user agents). The /games/ index is
JS-rendered, so game URLs are discovered from the All-in-One-SEO game sitemap.
Pages are WordPress marketing copy: the substantive prose lives in `.t_wysiwyg`
rich-text blocks (often duplicated for desktop/mobile). Value to the corpus is
new releases not yet on Tiltforums, plus edition (Premium/LE) and physical
playfield-feature descriptions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup

GAME_SITEMAP_URL = "https://sternpinball.com/game-sitemap.xml"
USER_AGENT = "pinguide-rag scraper (personal RAG project)"

# Absolute game page URLs, e.g. https://sternpinball.com/game/metallica-remastered/
GAME_URL_RE = re.compile(r"^https://sternpinball\.com/game/[a-z0-9-]+/?$")


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r


def slugify(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_").lower()
    return s or "unknown"


def fetch_urls() -> list[str]:
    xml_text = _get(GAME_SITEMAP_URL).text
    root = ET.fromstring(xml_text)
    # ElementTree uses Clark notation for namespaces; it resolves CDATA-wrapped
    # <loc> values transparently.
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    seen: set[str] = set()
    urls: list[str] = []
    for loc in root.findall(".//sm:loc", ns):
        url = (loc.text or "").strip()
        if GAME_URL_RE.match(url) and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_sections(soup: BeautifulSoup) -> dict[str, str]:
    """Extract the substantive prose from the page's rich-text blocks.

    Skips unrendered template literals ('{{ ... }}'), short chrome (newsletter
    blurb), and the duplicate desktop/mobile copies of the same block.
    """
    seen: set[str] = set()
    parts: list[str] = []
    for block in soup.select(".t_wysiwyg"):
        text = block.get_text("\n", strip=True)
        if not text or "{{" in text or len(text) < 80:
            continue
        key = text[:200]
        if key in seen:
            continue
        seen.add(key)
        parts.append(text)
    return {"Description": "\n\n".join(parts)} if parts else {}


def fetch_game(url: str) -> dict:
    soup = BeautifulSoup(_get(url).text, "html.parser")

    h1 = soup.find("h1")
    game = h1.get_text(strip=True) if h1 else ""
    if not game or "{{" in game:
        game = url.rstrip("/").split("/")[-1].replace("-", " ").title()

    sections = parse_sections(soup)
    return {"game": game, "manufacturer": "Stern", "url": url, "sections": sections}


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Stern Pinball game pages.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N game pages")
    parser.add_argument("--out", type=Path, default=Path("data/raw/stern"),
                        help="Output directory (default: data/raw/stern)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between requests (default: 2.0)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing files")
    args = parser.parse_args()

    print(f"Fetching game sitemap from {GAME_SITEMAP_URL}", flush=True)
    try:
        urls = fetch_urls()
    except Exception as e:
        print(f"ERROR fetching sitemap: {e}", file=sys.stderr)
        return 1
    print(f"Found {len(urls)} game URLs", flush=True)

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
            game = data["game"] or url.rstrip("/").split("/")[-1]

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
