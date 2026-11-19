"""Scrape Stern Pinball game pages and save per-game JSON to data/raw/stern/.

Stern's robots.txt permits general crawling (it blocks only GPTBot/ChatGPT-User/
Amazonbot/Scrapy, not ClaudeBot or default user agents). Game URLs are
discovered from the site sitemap.

Since late 2026 the site is a Next.js/Plasmic app: the visible HTML is empty
until JavaScript runs, but each game page embeds its CMS record (table
"gamePage") in the `__NEXT_DATA__` script's queryCache. We read the record
whose gameSlug matches the page: title, description, playfield features, and
per-edition (Pro/Premium/LE) cabinet features. Edition sub-pages
(/game/<slug>/premium/) are skipped because the main page already carries
every edition. Value to the corpus is new releases, edition differences, and
physical playfield-feature descriptions.
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

SITEMAP_URL = "https://www.sternpinball.com/sitemap.xml"
USER_AGENT = "pinguide-rag scraper (personal RAG project)"

# Main game page URLs only, e.g. https://www.sternpinball.com/game/metallica-remastered/
# (not edition sub-pages like .../metallica-remastered/premium/).
GAME_URL_RE = re.compile(r"^https://www\.sternpinball\.com/game/([a-z0-9-]+)/?$")


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


def _clean(text: object) -> str:
    """Collapse whitespace (including the odd U+2028 line separator) in CMS text."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _game_record(html: str, slug: str) -> dict:
    """Merge this game's gamePage CMS rows from the page's embedded queryCache.

    The page loads the record in several partial queries (and also loads other
    games for carousels), so keep only rows whose gameSlug matches, or whose
    query key filters on it, and merge their fields. For gameModels, prefer a
    copy whose 'Model Type' is expanded to a dict (it carries modelName).
    """
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None or not script.string:
        return {}
    cache = json.loads(script.string).get("props", {}).get("pageProps", {}).get("queryCache", {})

    where = f'"where":{{"gameSlug":"{slug}"}}'
    record: dict = {}
    for key, rows in cache.items():
        if '"table":"gamePage"' not in key or not isinstance(rows, list):
            continue
        for row in rows:
            # Each cached row is {id, createdAt, ..., data: {<fields>}}.
            row = row.get("data") if isinstance(row, dict) else None
            if not isinstance(row, dict):
                continue
            if row.get("gameSlug", slug) != slug or (where not in key and "gameSlug" not in row):
                continue
            for field, value in row.items():
                if field == "gameModels" and _has_model_names(value):
                    record[field] = value
                else:
                    record.setdefault(field, value)
    return record


def _has_model_names(models: object) -> bool:
    return isinstance(models, list) and any(
        isinstance((m.get("Game Model") or {}).get("Model Type"), dict)
        for m in models if isinstance(m, dict)
    )


def parse_sections(record: dict) -> dict[str, str]:
    """Turn the CMS record into the same section dict the other scrapers produce."""
    sections: dict[str, str] = {}

    description = "\n\n".join(
        t for t in (_clean(record.get("gameDescriptionLabel")), _clean(record.get("gameDescription"))) if t
    )
    if description:
        sections["Description"] = description

    features = []
    for item in record.get("gameFeatures") or []:
        f = (item or {}).get("feature") or {}
        title, desc = _clean(f.get("feature title")), _clean(f.get("feature description"))
        if title or desc:
            features.append(f"{title}: {desc}" if title and desc else title or desc)
    if features:
        sections["Gameplay Features"] = "\n".join(f"- {f}" for f in features)

    editions = []
    for item in record.get("gameModels") or []:
        model = (item or {}).get("Game Model") or {}
        model_type = model.get("Model Type")
        name = _clean(model_type.get("modelName")) if isinstance(model_type, dict) else ""
        cab = [_clean(c.get("Feature")) for c in model.get("Cabinet Features") or [] if isinstance(c, dict)]
        cab = [c for c in cab if c]
        if cab:
            editions.append(f"{name or 'Model'}:\n" + "\n".join(f"- {c}" for c in cab))
    if editions:
        sections["Editions"] = "\n\n".join(editions)

    return sections


def fetch_game(url: str) -> dict:
    slug = GAME_URL_RE.match(url).group(1)
    record = _game_record(_get(url).text, slug)
    game = _clean(record.get("gameTitle")) or slug.replace("-", " ").title()
    return {"game": game, "manufacturer": "Stern", "url": url, "sections": parse_sections(record)}


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
