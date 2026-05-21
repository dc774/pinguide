"""Scrape the PAPA/Replay Foundation rulesheet archive and save per-game JSON to data/raw/papa/."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

# papapinball.com is down; the same archive is mirrored here.
INDEX_URL = "https://www.replayfoundation.org/papa/learning-center/player-guide/rule-sheets/"
USER_AGENT = "pinguide-rag scraper (personal RAG project)"

RULESHEET_URL_RE = re.compile(r"^https?://(?:www\.)?pinball\.org/rules/[^/]+\.html$")
TITLE_SUFFIX_RE = re.compile(
    r"\s*[-|]\s*(?:pinball\.org|PAPA|Replay Foundation)\s*$", re.IGNORECASE
)
PHP_TITLE_RE = re.compile(r'\$title\s*=\s*["\'](.+?)["\']')

MANUFACTURER_RE = re.compile(
    # Combined forms must come before the individual names so they match first.
    r"\b(Williams/Bally|Bally/Williams|Williams|Bally|Data East|Gottlieb|Stern|Sega|Capcom|Alvin G(?:\.[,\s])?)\b",
    re.IGNORECASE,
)


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r


def slugify(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_").lower()
    return s or "unknown"


def _normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/"), "", ""))


def fetch_index() -> list[str]:
    soup = BeautifulSoup(_get(INDEX_URL).text, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not RULESHEET_URL_RE.match(href):
            continue
        normalized = _normalize_url(href)
        if normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


def extract_game_title(soup: BeautifulSoup, url: str, raw: str = "") -> str:
    # PHP source is visible in the raw response on these old pages — parse it first.
    if raw:
        m = PHP_TITLE_RE.search(raw)
        if m:
            return m.group(1).strip()

    # Try <title> tag — use get_text() not .string; .string returns None if the
    # tag has nested elements (common on older pages).
    if soup.title:
        title = TITLE_SUFFIX_RE.sub("", soup.title.get_text(strip=True)).strip()
        if title:
            return title

    # Try first <h1>
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(strip=True)
        if text:
            return text

    # Try first <h2>
    h2 = soup.find("h2")
    if h2:
        text = h2.get_text(strip=True)
        if text:
            return text

    # Derive from URL slug
    slug = urlsplit(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.html?$", "", slug)
    return " ".join(w.capitalize() for w in slug.split("-") if w)


def extract_manufacturer(text: str) -> str:
    m = MANUFACTURER_RE.search(text[:2000])
    if m:
        raw = m.group(1)
        # Normalise "Alvin G." variants
        if raw.lower().startswith("alvin"):
            return "Alvin G."
        return raw
    return ""


def parse_sections(soup: BeautifulSoup) -> dict[str, str]:
    body = soup.body or soup
    sections: dict[str, list[str]] = {}
    current = "Introduction"

    for child in body.children:
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if text:
                sections.setdefault(current, []).append(text)
            continue
        if not isinstance(child, Tag):
            continue
        if child.name == "h2":
            current = child.get_text(strip=True) or current
            continue
        text = child.get_text("\n", strip=True)
        if text:
            sections.setdefault(current, []).append(text)

    return {k: "\n\n".join(v) for k, v in sections.items() if v}


def fetch_rulesheet(url: str) -> dict:
    raw = _get(url).text
    soup = BeautifulSoup(raw, "html.parser")
    game = extract_game_title(soup, url, raw)
    body_text = soup.get_text(" ", strip=True)
    manufacturer = extract_manufacturer(body_text)
    sections = parse_sections(soup)
    return {"game": game, "manufacturer": manufacturer, "url": url, "sections": sections}


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape PAPA/pinball.org rulesheets.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N rulesheet links")
    parser.add_argument("--out", type=Path, default=Path("data/raw/papa"),
                        help="Output directory (default: data/raw/papa)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between requests (default: 2.0)")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape even when output file already exists")
    args = parser.parse_args()

    print(f"Fetching index from {INDEX_URL}", flush=True)
    try:
        urls = fetch_index()
    except Exception as e:
        print(f"ERROR fetching index: {e}", file=sys.stderr)
        return 1
    print(f"Found {len(urls)} rulesheet links", flush=True)

    if args.limit is not None:
        urls = urls[: args.limit]
        print(f"Limiting to first {len(urls)}", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)

    saved = skipped = errors = 0
    total = len(urls)

    for i, url in enumerate(urls, 1):
        prefix = f"[{i}/{total}]"

        # Skip-check: guess filename from URL slug before fetching
        slug = urlsplit(url).path.rstrip("/").split("/")[-1]
        slug = re.sub(r"\.html?$", "", slug)
        guess_path = args.out / f"{slugify(slug)}.json"
        if guess_path.exists() and not args.force:
            print(f"{prefix} {slug} — skipped (exists)", flush=True)
            skipped += 1
            continue

        try:
            data = fetch_rulesheet(url)
            out_path = args.out / f"{slugify(data['game'])}.json"
            out_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"{prefix} {data['game']} — saved", flush=True)
            saved += 1
        except Exception as e:
            print(f"{prefix} {url} — ERROR: {e}", file=sys.stderr, flush=True)
            errors += 1

        time.sleep(args.delay)

    print(f"Done. saved={saved} skipped={skipped} errors={errors}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
