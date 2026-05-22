"""Scrape Tiltforums rulesheet wiki pages and save per-game JSON to data/raw/."""

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

CATEGORY_URL = "https://tiltforums.com/c/game-specific/rulesheet-wikis/18"
MASTER_LIST_URL = "https://tiltforums.com/t/rulesheet-master-list/7230"
USER_AGENT = "pinguide-rag scraper (personal RAG project)"

TITLE_SUFFIX_RE = re.compile(r"\s*\(?\s*(?:wiki|rulesheet)s?\s*\)?\s*$", re.IGNORECASE)
URL_SLUG_SUFFIX_RE = re.compile(r"[-_](?:rulesheet|wiki)$", re.IGNORECASE)
MFR_PINBALL_SUFFIX_RE = re.compile(r"\s+pinball\s*$", re.IGNORECASE)
_TILTFORUMS_TOPIC_RE = re.compile(r"^https?://tiltforums\.com/t/[^/]+/\d+/?$")

# Non-game meta topics in the rulesheet category — skip these
_SKIP_TOPIC_IDS: frozenset[int] = frozenset({
    7230,  # Rulesheet Master List
    457,   # About the Wiki Rulesheets category
    7040,  # Games With Lock & Point Stealing
    9237,  # Action Button Master List
    5399,  # List of Exploits Allowed/Disallowed in Tournament Play
    3615,  # List of games with their current code number
    9991,  # New Competition ROMs
    2725,  # Points for Extra Ball Wiki
    4827,  # Game of Thrones Casual Mode rules? (discussion, not rulesheet)
})


def _get_json(url: str) -> dict:
    json_url = url.rstrip("/") + ".json"
    r = requests.get(json_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.json()


def _normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/"), "", ""))


def slugify(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_").lower()
    return s or "unknown"


def _likely_filename(url: str) -> str:
    """Best-effort guess of output filename from URL slug, used only for skip-check."""
    parts = url.rstrip("/").split("/")
    slug = parts[-2] if len(parts) >= 2 else ""
    prev = None
    while slug != prev:
        prev = slug
        slug = URL_SLUG_SUFFIX_RE.sub("", slug)
    return f"{slugify(slug)}.json"


def clean_game_title(title: str) -> str:
    cleaned = title.strip()
    prev = None
    while cleaned != prev:
        prev = cleaned
        cleaned = TITLE_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned


def fetch_category_entries() -> list[dict]:
    """Paginate the rulesheet wiki category and return one entry per game topic."""
    entries: list[dict] = []
    seen_ids: set[int] = set()
    page = 0

    while True:
        r = requests.get(
            f"{CATEGORY_URL}.json?page={page}",
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        topics = data.get("topic_list", {}).get("topics", [])
        if not topics:
            break

        for topic in topics:
            topic_id = topic.get("id")
            if not topic_id or topic_id in _SKIP_TOPIC_IDS or topic_id in seen_ids:
                continue
            seen_ids.add(topic_id)
            slug = topic.get("slug", "")
            url = _normalize_url(f"https://tiltforums.com/t/{slug}/{topic_id}")
            entries.append({"url": url, "manufacturer": ""})

        if not data.get("topic_list", {}).get("more_topics_url"):
            break
        page += 1

    return entries


def fetch_master_list_manufacturers() -> dict[str, str]:
    """Return {normalized_url: manufacturer} from the master list for manufacturer lookup."""
    data = _get_json(MASTER_LIST_URL)
    cooked = data["post_stream"]["posts"][0]["cooked"]
    soup = BeautifulSoup(cooked, "html.parser")

    result: dict[str, str] = {}
    current_mfr = ""

    for el in soup.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name == "h2":
            raw = el.get_text(strip=True)
            current_mfr = MFR_PINBALL_SUFFIX_RE.sub("", raw).strip(": ") or raw
        elif el.name == "a":
            href = el.get("href") or ""
            parts = urlsplit(href)
            cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            if not _TILTFORUMS_TOPIC_RE.match(cleaned):
                continue
            result[_normalize_url(href)] = current_mfr

    return result


def parse_sections(cooked_html: str) -> dict:
    soup = BeautifulSoup(cooked_html, "html.parser")
    sections: dict[str, list[str]] = {}
    current = "Introduction"

    for child in soup.children:
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


def fetch_rulesheet(url: str, manufacturer: str) -> dict:
    data = _get_json(url)
    title = (data.get("title") or "").strip()
    posts = data.get("post_stream", {}).get("posts", [])
    if not posts:
        raise ValueError("topic has no posts")
    cooked = posts[0].get("cooked") or ""
    if not cooked:
        raise ValueError("first post is empty")
    return {
        "game": clean_game_title(title),
        "manufacturer": manufacturer,
        "url": url,
        "sections": parse_sections(cooked),
    }


def save_rulesheet(data: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slugify(data['game'])}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Tiltforums rulesheet wikis.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N rulesheet links")
    parser.add_argument("--out", type=Path, default=Path("data/raw"),
                        help="Output directory (default: data/raw)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between requests (default: 2.0)")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape even when output file already exists")
    args = parser.parse_args()

    print(f"Fetching rulesheet category from {CATEGORY_URL}", flush=True)
    try:
        entries = fetch_category_entries()
    except Exception as e:
        print(f"ERROR fetching category: {e}", file=sys.stderr)
        return 1
    print(f"Found {len(entries)} game topics in category", flush=True)

    print("Fetching master list for manufacturer info...", flush=True)
    try:
        mfr_map = fetch_master_list_manufacturers()
        for entry in entries:
            entry["manufacturer"] = mfr_map.get(entry["url"], "")
        matched = sum(1 for e in entries if e["manufacturer"])
        print(f"  Manufacturer resolved for {matched}/{len(entries)} games", flush=True)
    except Exception as e:
        print(f"  WARNING: could not fetch master list ({e}) — manufacturer will be empty",
              file=sys.stderr)

    if args.limit is not None:
        entries = entries[: args.limit]
        print(f"Limiting to first {len(entries)}", flush=True)

    saved = skipped = errors = 0
    total = len(entries)

    for i, entry in enumerate(entries, 1):
        prefix = f"[{i}/{total}]"
        url = entry["url"]

        if not args.force:
            guess_path = args.out / _likely_filename(url)
            if guess_path.exists():
                print(f"{prefix} {guess_path.stem} — skipped (exists)", flush=True)
                skipped += 1
                continue

        try:
            data = fetch_rulesheet(url, entry["manufacturer"])
            save_rulesheet(data, args.out)
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
