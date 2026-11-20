"""Import game profiles from the Kineticist API and save per-game JSON.

Kineticist (kineticist.com) publishes its pinball database as an official API,
free during early access. Catalog reads need no key (60 requests/minute and
2,000/day per IP). Its terms permit personal projects and require visible
attribution ("Data provided by Kineticist", linked), which the frontend footer
carries. Docs: https://www.kineticist.com/docs/api

Per game we save a profile (description, years, manufacturer, tags), the design
team, the editions (MSRP, production run), and, when the game has a "Keys to
the game" tutorial image, a text transcription of it made once with Claude
(vision). Transcripts are reused from the existing file on later runs, so the
image cost is paid only once per image.

Names: Kineticist repeats titles across eras ("Godzilla" by Sega in 1998 and by
Stern in 2021). A title that appears more than once is saved as
"Title (Maker, Year)" so the machines stay distinct. The ingest merges these
profiles onto rulesheet machines by OPDB ID or rulesheet link first, then by
name (see ingest/pipeline.py).
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests
from PIL import Image

API = "https://www.kineticist.com/api/v1"
USER_AGENT = "pinguide-rag scraper (personal RAG project)"
DELAY = 1.1  # seconds between API calls: stays under 60 requests/minute
TRANSCRIBE_MODEL = "claude-haiku-5-5"
TRANSCRIBE_PROMPT = (
    "This image is a pinball game tutorial ('keys to the game'). Transcribe all of its text "
    "as plain text: keep the heading, and one '- ' bullet per item, worded exactly as in the "
    "image. Do not add, explain or summarize anything. If the image has no readable rules "
    "text, reply with exactly: NONE"
)
_CORP_SUFFIX_RE = re.compile(r",?\s+(?:&\s+)?(?:inc|ltd|llc|co|corp|gmbh|b\.?v)\.?$", re.IGNORECASE)


class DailyLimitReached(Exception):
    pass


def _api(path: str) -> dict:
    time.sleep(DELAY)
    r = requests.get(f"{API}{path}", headers={"User-Agent": USER_AGENT}, timeout=30)
    if r.status_code == 429:
        raise DailyLimitReached(r.text[:300])
    r.raise_for_status()
    return r.json()


def list_games() -> list[dict]:
    games: list[dict] = []
    offset = 0
    while True:
        page = _api(f"/games?limit=100&offset={offset}")
        games.extend(page["data"])
        if not page.get("pagination", {}).get("has_more"):
            return games
        offset += 100


def opdb_group(opdb_id: str | None) -> str:
    """'GweeP-MW95j', 'GweeP' and 'weeP' all -> 'weeP' (the OPDB group ID)."""
    group = (opdb_id or "").split("-", 1)[0]
    return group[1:] if len(group) == 5 and group.startswith("G") else group


def _maker(game: dict) -> str:
    name = (game.get("manufacturer") or {}).get("name") or ""
    name = re.sub(r"\s*\([^)]*\)", "", name)  # "Sega Pinball Inc. (US)" -> "Sega Pinball Inc."
    return _CORP_SUFFIX_RE.sub("", name).strip()


def display_names(games: list[dict]) -> dict[str, str]:
    """slug -> name, adding '(Maker, Year)' to titles Kineticist uses more than once."""
    counts = Counter(g["name"].strip().lower() for g in games)
    names = {}
    for g in games:
        name = g["name"].strip()
        if counts[name.lower()] > 1:
            detail = ", ".join(str(x) for x in (_maker(g), g.get("first_manufacture_year")) if x)
            name = f"{name} ({detail})" if detail else name
        names[g["slug"]] = name
    # Same title, maker and year (e.g. Zaccaria's EM and solid-state "Ski Jump",
    # both 1978): add the game type, or the slug as a last resort.
    taken = Counter(names.values())
    for g in games:
        name = names[g["slug"]]
        if taken[name] > 1 and name.endswith(")"):
            names[g["slug"]] = f"{name[:-1]}, {g.get('game_type') or g['slug']})"
    return names


def _plain(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(html.unescape(text).split())


def _money(msrp: str | None) -> str:
    try:
        return f"${int(float(msrp)):,}"
    except (TypeError, ValueError):
        return ""


def _image_type(data: bytes) -> str | None:
    """Media type from the file's magic bytes (some images are served with a wrong
    Content-Type header, which the API rejects)."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def transcribe(client, image_url: str) -> str:
    """Return the tutorial image's text, or '' if it has none."""
    r = requests.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    data, media_type = r.content, _image_type(r.content)
    if media_type is None:
        # e.g. AVIF, which the API doesn't accept: convert to PNG.
        try:
            with Image.open(io.BytesIO(data)) as img:
                buf = io.BytesIO()
                img.save(buf, format="PNG")
            data, media_type = buf.getvalue(), "image/png"
        except Exception as e:
            print(f"  skipping {image_url}: unreadable image ({e})", flush=True)
            return ""
    msg = client.messages.create(
        model=TRANSCRIBE_MODEL,
        max_tokens=4096,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(data).decode()}},
            {"type": "text", "text": TRANSCRIBE_PROMPT},
        ]}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return "" if text == "NONE" else text


def build_record(d: dict, name: str, tutorials: list[dict]) -> dict:
    years = "–".join(str(y) for y in dict.fromkeys(
        (d.get("first_manufacture_year"), d.get("last_manufacture_year"))) if y)
    profile = [_plain(d.get("description"))]
    facts = [
        ("Manufacturer", (d.get("manufacturer") or {}).get("name")),
        ("Years made", years),
        ("In production", "yes" if d.get("in_production") else "no"),
        ("Type", d.get("game_type")),
        ("Display", d.get("display_type")),
        ("Players", d.get("player_count")),
        ("Tags", ", ".join(t["name"] for t in d.get("tags") or [])),
    ]
    profile.append("\n".join(f"- {k}: {v}" for k, v in facts if v))

    credits = [f"- {c['role_name']}: {c['person']['name']}" for c in d.get("credits") or []
               if c.get("person")]

    editions = []
    for e in d.get("editions") or []:
        bits = [x for x in (
            f"MSRP {_money(e.get('msrp'))}" if _money(e.get("msrp")) else "",
            f"{e['production_run']:,} made" if e.get("production_run") else "",
            f"released {e['manufacture_date']}" if e.get("manufacture_date") else "",
            f"model number {e['model_number']}" if e.get("model_number") else "",
            e.get("mpu") or "",
        ) if x]
        editions.append(f"- {e['name']}" + (f": {', '.join(bits)}" if bits else ""))

    sections = {"Profile": "\n\n".join(p for p in profile if p)}
    if credits:
        sections["Design Team"] = "\n".join(credits)
    if editions:
        sections["Editions"] = "\n".join(editions)
    tutorial_text = "\n\n".join(t["text"] for t in tutorials if t["text"])
    if tutorial_text:
        sections["Getting Started (Kineticist tutorial)"] = tutorial_text

    return {
        "game": name,
        "manufacturer": (d.get("manufacturer") or {}).get("name") or "",
        "url": d.get("url") or f"https://www.kineticist.com/games/pinball/{d['slug']}",
        "sections": sections,
        # Matching keys for the ingest (not shown to users).
        "opdb_group": opdb_group(d.get("opdb_id")),
        "rules_url": d.get("game_rules_link") or "",
        "tutorials": tutorials,  # [{image, text}], cached so images are transcribed once
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import game profiles from the Kineticist API.")
    parser.add_argument("--out", type=Path, default=Path("data/raw/kineticist"))
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N games")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch games that already have a file (tutorial transcripts are kept)")
    parser.add_argument("--no-tutorials", action="store_true",
                        help="Skip transcribing tutorial images (no Anthropic API calls)")
    args = parser.parse_args()

    client = None
    if not args.no_tutorials:
        import anthropic
        from dotenv import load_dotenv
        load_dotenv(override=True)  # same as ingest/pipeline.py
        client = anthropic.Anthropic()

    print("Listing games...", flush=True)
    try:
        games = list_games()
    except Exception as e:
        print(f"ERROR listing games: {e}", file=sys.stderr)
        return 1
    names = display_names(games)
    print(f"Found {len(games)} games", flush=True)
    if args.limit is not None:
        games = games[: args.limit]

    args.out.mkdir(parents=True, exist_ok=True)
    saved = skipped = errors = transcribed = 0
    for i, g in enumerate(games, 1):
        path = args.out / f"{g['slug']}.json"
        prefix = f"[{i}/{len(games)}] {names[g['slug']]}"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if old is not None and not args.force:
            skipped += 1
            continue
        try:
            d = _api(f"/games/{g['slug']}?include=credits")["data"]
            cached = {t["image"]: t["text"] for t in (old or {}).get("tutorials", [])}
            tutorials = []
            for url in (d.get("images") or {}).get("tutorial") or []:
                if url not in cached and client is not None:
                    cached[url] = transcribe(client, url)
                    transcribed += 1
                if url in cached:
                    tutorials.append({"image": url, "text": cached[url]})
            record = build_record(d, names[g["slug"]], tutorials)
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            saved += 1
            print(f"{prefix}: saved" + (f" (+{len(tutorials)} tutorial)" if tutorials else ""), flush=True)
        except DailyLimitReached as e:
            print(f"Rate limit reached, stopping. Re-run later to resume. ({e})", flush=True)
            break
        except Exception as e:
            print(f"{prefix}: ERROR {e}", file=sys.stderr, flush=True)
            errors += 1

    print(f"Done. saved={saved} skipped={skipped} errors={errors} "
          f"images_transcribed={transcribed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
