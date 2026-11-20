"""Import rulesheets from pinballrulesheets.com and save per-game JSON.

pinballrulesheets.com is the successor to the Tiltforums wiki rulesheets
(Tiltforums closed on 2026-09-01). Its source is the GitHub repo
heyrocker/pinballrules, licensed CC0 (public domain). Instead of crawling the
site, we download the repo once as a tarball and read the Markdown files from
memory (nothing is extracted to disk).

Each rulesheet is docs/<manufacturer>/<slug>.md with YAML-style front matter
(title, manufacturer) followed by Markdown split into "## " sections, the same
section granularity the Tiltforums scraper used. Non-rulesheet pages
(docs/contribute, docs/other, index/master-list pages) are skipped.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tarfile
from pathlib import Path

import requests

from scraper.kineticist import opdb_group

REPO = "heyrocker/pinballrules"
TARBALL_URL = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/main"
SITE_URL = "https://pinballrulesheets.com"
USER_AGENT = "pinguide-rag scraper (personal RAG project)"

# docs/<manufacturer>/<slug>.md. Directories that hold site or reference pages,
# not machine rulesheets, are skipped.
_SKIP_DIRS = {"contribute", "other", "assets", "_layouts"}
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING_ANCHOR_RE = re.compile(r"\s*\{#[^}]*\}\s*$")
# Trailing words that describe the page rather than name the machine.
_TITLE_TAIL_RE = re.compile(r"\s*[(\[]?\b(?:rulesheet|rules|wiki|wip)\b[)\]]?\s*$", re.IGNORECASE)
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.IGNORECASE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_").lower()
    return s or "unknown"


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            # YAML double-quoted strings allow escapes like ’; JSON decodes them.
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        meta[key.strip()] = value.strip("'")
    return meta, text[m.end():]


def clean_title(title: str) -> str:
    """'Fallout Pinball Rulesheet' -> 'Fallout Pinball', 'Hot Wheels Rulesheet (WIP)' -> 'Hot Wheels'."""
    prev = None
    while title != prev:
        prev = title
        title = _TITLE_TAIL_RE.sub("", title).strip(" -:")
    return title


def _clean_body(text: str) -> str:
    text = _IMG_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _TAG_RE.sub("", text)
    # Drop table rows left empty once images are removed (e.g. "|  |  |", "|---|---|").
    lines = [ln for ln in text.splitlines() if not re.fullmatch(r"[\s|:\-]*\|[\s|:\-]*", ln)]
    # A table reduced to just its header row (e.g. "| Pro | Premium/LE |" above
    # removed images) carries no information.
    content = [ln for ln in lines if ln.strip()]
    if len(content) == 1 and content[0].lstrip().startswith("|"):
        return ""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_sections(body: str) -> dict[str, str]:
    """Split Markdown on '## ' headings; deeper headings stay inside their section."""
    sections: dict[str, list[str]] = {}
    current = "Introduction"
    for line in body.splitlines():
        if line.startswith("## "):
            current = _HEADING_ANCHOR_RE.sub("", line[3:]).strip() or current
            continue
        if line.startswith("# "):  # page title, repeated from front matter
            continue
        line = _HEADING_ANCHOR_RE.sub("", line) if line.startswith("#") else line
        sections.setdefault(current, []).append(line)
    sections.pop("Table of Contents", None)
    cleaned = {name: _clean_body("\n".join(lines)) for name, lines in sections.items()}
    return {name: text for name, text in cleaned.items() if text}


def parse_rulesheet(rel_path: str, text: str) -> dict | None:
    """rel_path is relative to docs/, e.g. 'stern/fallout-rulesheet.md'."""
    meta, body = _front_matter(text)
    title = clean_title(meta.get("title", ""))
    if not title:
        return None
    return {
        "game": title,
        "manufacturer": meta.get("manufacturer", ""),
        "url": f"{SITE_URL}/{rel_path[:-3]}",
        "sections": parse_sections(body),
        # OPDB group ID: de-duplicates rulesheets here, and lets the ingest match
        # Kineticist profiles to this machine exactly.
        "opdb_group": opdb_group(meta.get("opdb_id")),
    }


def iter_rulesheets(tar_bytes: bytes):
    """Yield (path relative to docs/, Markdown text) for each rulesheet in the tarball."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".md"):
                continue
            # <repo>-<branch>/docs/<manufacturer>/<file>.md
            parts = member.name.split("/")
            if len(parts) != 4 or parts[1] != "docs" or parts[2] in _SKIP_DIRS:
                continue
            f = tar.extractfile(member)
            if f is not None:
                yield f"{parts[2]}/{parts[3]}", f.read().decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import rulesheets from pinballrulesheets.com (CC0).")
    parser.add_argument("--out", type=Path, default=Path("data/raw/pinballrules"),
                        help="Output directory (default: data/raw/pinballrules)")
    args = parser.parse_args()

    print(f"Downloading {TARBALL_URL}", flush=True)
    try:
        r = requests.get(TARBALL_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
        r.raise_for_status()
    except Exception as e:
        print(f"ERROR downloading repo: {e}", file=sys.stderr)
        return 1

    # Some machines have two files under different titles (e.g. "Sharkey's
    # Shootout" and "Creating a Rulesheet for Sharkey's Shootout"). Files with the
    # same opdb_id (or, without one, the same title) are the same machine: keep
    # the one with the most content, named with the shortest title.
    by_machine: dict[str, dict] = {}
    empty = 0
    for rel_path, text in iter_rulesheets(r.content):
        data = parse_rulesheet(rel_path, text)
        if not data or not data["sections"]:
            print(f"  {rel_path}: no content, skipped", flush=True)
            empty += 1
            continue
        key = data["opdb_group"] or slugify(data["game"])
        prev = by_machine.get(key)
        if prev is None:
            by_machine[key] = data
            continue
        keep = max(prev, data, key=lambda d: sum(len(s) for s in d["sections"].values()))
        keep["game"] = min(prev["game"], data["game"], key=len)
        print(f"  duplicate {keep['game']!r}: keeping {keep['url']}", flush=True)
        by_machine[key] = keep
    by_file = {f"{slugify(d['game'])}.json": d for d in by_machine.values()}
    if not by_file:
        # Never wipe the existing files because of an empty or changed download.
        print("ERROR: no rulesheets found in the download; nothing written", file=sys.stderr)
        return 1

    # The repo is the source of truth: rewrite the whole directory so renamed or
    # removed rulesheets don't linger.
    args.out.mkdir(parents=True, exist_ok=True)
    for old in args.out.glob("*.json"):
        if old.name not in by_file:
            old.unlink()
    for name, data in sorted(by_file.items()):
        (args.out / name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Done. saved={len(by_file)} empty={empty}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
