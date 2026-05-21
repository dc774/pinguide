"""Scrape Bob's Guide (silverballmania.com) and save per-game JSON to data/raw/bobs/."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

SITEMAP_URL = "https://rules.silverballmania.com/sitemap.xml"
USER_AGENT = "pinguide-rag scraper (personal RAG project)"

# Matches absolute game page URLs from the sitemap
RULES_URL_RE = re.compile(
    r"^https://rules\.silverballmania\.com/rules/[A-Za-z0-9_-]+$"
)
# Handles both • (U+2022 BULLET) and · (U+00B7 MIDDLE DOT)
BULLET_RE = re.compile(r"\s*[•·]\s*")

# These h2 sections are page chrome, not rulesheet content
SKIP_SECTIONS = {"External Links", "Machine Information"}


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r


def slugify(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_").lower()
    return s or "unknown"


def fetch_urls() -> list[str]:
    xml_text = _get(SITEMAP_URL).text
    root = ET.fromstring(xml_text)
    # ElementTree uses Clark notation for namespaces
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    seen: set[str] = set()
    urls: list[str] = []
    for loc in root.findall(".//sm:loc", ns):
        url = (loc.text or "").strip()
        if RULES_URL_RE.match(url) and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_meta(soup: BeautifulSoup) -> tuple[str, str]:
    """Return (manufacturer, year) parsed from the bullet-separated metadata line."""
    h1 = soup.find("h1")
    if not h1:
        return "", ""

    # Look for the bullet string in elements that follow the h1 in document order
    for candidate in h1.find_next_siblings():
        text = candidate.get_text(" ", strip=True)
        if BULLET_RE.search(text):
            parts = BULLET_RE.split(text)
            manufacturer = parts[0].strip() if len(parts) > 0 else ""
            year = parts[1].strip() if len(parts) > 1 else ""
            return manufacturer, year

    # Fallback: scan the parent's children for any NavigableString with a bullet
    if h1.parent:
        for sibling in h1.parent.children:
            if isinstance(sibling, NavigableString):
                text = str(sibling).strip()
                if BULLET_RE.search(text):
                    parts = BULLET_RE.split(text)
                    manufacturer = parts[0].strip() if len(parts) > 0 else ""
                    year = parts[1].strip() if len(parts) > 1 else ""
                    return manufacturer, year

    return "", ""


def parse_sections(soup: BeautifulSoup) -> dict[str, str]:
    body = soup.body or soup
    sections: dict[str, list[str]] = {}
    current = "Introduction"
    skip = False

    for child in body.children:
        if isinstance(child, NavigableString):
            if not skip:
                text = str(child).strip()
                if text:
                    sections.setdefault(current, []).append(text)
            continue
        if not isinstance(child, Tag):
            continue
        if child.name == "h2":
            heading = child.get_text(strip=True)
            if heading in SKIP_SECTIONS:
                skip = True
            else:
                skip = False
                current = heading or current
            continue
        if skip:
            continue
        text = child.get_text("\n", strip=True)
        if text:
            sections.setdefault(current, []).append(text)

    return {k: "\n\n".join(v) for k, v in sections.items() if v}


def fetch_game(url: str) -> dict:
    soup = BeautifulSoup(_get(url).text, "html.parser")

    h1 = soup.find("h1")
    game = h1.get_text(strip=True) if h1 else ""

    manufacturer, _year = parse_meta(soup)
    sections = parse_sections(soup)

    return {"game": game, "manufacturer": manufacturer, "url": url, "sections": sections}


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Bob's Guide rulesheets.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N game pages")
    parser.add_argument("--out", type=Path, default=Path("data/raw/bobs"),
                        help="Output directory (default: data/raw/bobs)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between requests (default: 2.0)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing files")
    args = parser.parse_args()

    print(f"Fetching sitemap from {SITEMAP_URL}", flush=True)
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

    saved = skipped = errors = 0
    total = len(urls)

    for i, url in enumerate(urls, 1):
        prefix = f"[{i}/{total}]"
        try:
            data = fetch_game(url)
            game = data["game"] or url.split("/")[-1]
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

    print(f"Done. saved={saved} skipped={skipped} errors={errors}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
