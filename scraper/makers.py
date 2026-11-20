"""Scrape smaller manufacturers' sites and save per-game JSON to data/raw/<maker>/.

Covers Multimorphic, Spooky, Pinball Brothers, Dutch Pinball, Turner, American
Pinball and Barrels of Fun. (Jersey Jack's Shopify store rate-limits scripts on
sight, so it is not included.) All of their robots.txt files allow
general crawling and none signals against AI use (checked 2026-11-20); every
request is also checked against robots.txt at run time.

Games are discovered from each site's own listing pages, sitemap or feed. Where
a site has no clean signal for "this page is a game" (Spooky's page-builder
pages, Barrels of Fun's code-update posts),
the maker's game names come from the Kineticist import, so run
`python -m scraper.kineticist` first.

Value: machines the rulesheet sources lack (mostly new releases and P3 games)
and code update notes. Marketing depth, not full rules.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

USER_AGENT = "pinguide-rag scraper (personal RAG project)"
DELAY = 2.0
KINETICIST_DIR = Path("data/raw/kineticist")
_robots: dict[str, urllib.robotparser.RobotFileParser] = {}
_BLOCK_TAGS = {"div", "p", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "table",
               "section", "article", "figure", "blockquote", "form"}
# Lines that are page furniture, not game information: cookie banners, store
# "related products" tiles, and theme placeholder text.
_JUNK = ("cookie", "add to cart", "lorem ipsum")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _get(url: str) -> requests.Response:
    host = "{0.scheme}://{0.netloc}".format(urlsplit(url))
    if host not in _robots:
        # Fetch robots.txt ourselves: RobotFileParser.read() uses Python's default
        # User-Agent, which some sites refuse, and then it blocks everything.
        rp = urllib.robotparser.RobotFileParser()
        r = requests.get(f"{host}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=30)
        rp.parse(r.text.splitlines() if r.ok else [])
        _robots[host] = rp
    if not _robots[host].can_fetch(USER_AGENT, url):
        raise PermissionError(f"robots.txt disallows {url}")
    time.sleep(DELAY)
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r


def _soup(url: str) -> BeautifulSoup:
    # Pass bytes so the parser detects the page's encoding from its <meta> tag.
    return BeautifulSoup(_get(url).content, "html.parser")


def _clean(text: str) -> str:
    return " ".join(html.unescape(text or "").split())


def _strip_html(fragment: str) -> str:
    return _clean(re.sub(r"<[^>]+>", " ", fragment or ""))


def slugify(title: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_").lower() or "unknown"


def _key(name: str) -> str:
    """Loose title key: lowercase letters and digits only ("Scooby-Doo!" -> "scoobydoo")."""
    return re.sub(r"[^a-z0-9]", "", html.unescape(name).lower())


def page_title(soup: BeautifulSoup) -> str:
    """The game name from <title> ("Houdini - American Pinball" -> "Houdini").

    The <title> is more reliable than <h1>, which is often a slogan.
    """
    title = _clean(soup.title.get_text(" ") if soup.title else "")
    # Drop only the last " | Site name" part, so "Lexy Lightspeed – Escape From Earth"
    # keeps its subtitle.
    title = re.sub(r"\s+[|\u2013\u2014-](?:\s+[^|\u2013\u2014]*)?$", "", title)
    title = re.sub(r"[™®©]", "", title)
    return re.sub(r"\s+pinball machine$", "", title, flags=re.IGNORECASE).strip()


def page_sections(soup: BeautifulSoup) -> dict[str, str]:
    """Readable text split at h2/h3 headings, without menus, banners and repeats."""
    for tag in soup(["script", "style", "nav", "header", "footer", "form", "noscript",
                     "svg", "iframe", "button"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    sections: dict[str, list[str]] = {}
    current, seen = "Overview", set()
    for el in root.find_all(["h2", "h3", "p", "li", "div"]):
        # Some page builders put prose straight in <div>s. For a div, take only
        # its own text (loose text and inline tags); nested blocks are visited
        # on their own, so nothing is collected twice.
        if el.name == "div":
            text = _clean(" ".join(
                c.get_text(" ") if isinstance(c, Tag) else str(c)
                for c in el.children
                # type() check: skips Comment/CData, which subclass NavigableString
                if (isinstance(c, Tag) and c.name not in _BLOCK_TAGS) or type(c) is NavigableString))
        else:
            text = _clean(el.get_text(" "))
        if el.name in ("h2", "h3"):
            if 2 < len(text) < 80:
                current = text
            continue
        low = text.lower()
        if len(text) < 40 or text in seen or any(junk in low for junk in _JUNK):
            continue
        seen.add(text)
        sections.setdefault(current, []).append(f"- {text}" if el.name == "li" else text)
    return {k: "\n".join(v) for k, v in sections.items() if v}


def _record(game: str, manufacturer: str, url: str, sections: dict[str, str]) -> dict:
    return {"game": game, "manufacturer": manufacturer, "url": url, "sections": sections}


def _content_links(url: str, pattern: str) -> list[str]:
    """Same-site links in a page's main content (menus removed) whose path matches pattern."""
    soup = _soup(url)
    for tag in soup(["nav", "header", "footer"]):
        tag.decompose()
    host = urlsplit(url).netloc
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        link = urljoin(url, a["href"]).split("#")[0].split("?")[0]
        if urlsplit(link).netloc == host and re.search(pattern, urlsplit(link).path) and link not in out:
            out.append(link)
    return out


def _page_record(url: str, manufacturer: str) -> dict | None:
    soup = _soup(url)
    title = page_title(soup)
    sections = page_sections(soup)
    return _record(title, manufacturer, url, sections) if title and sections else None


def kineticist_games(maker: str) -> list[str]:
    """Game names Kineticist lists for a manufacturer (case-insensitive substring match)."""
    names = []
    for path in KINETICIST_DIR.glob("*.json"):
        d = json.loads(path.read_text(encoding="utf-8"))
        if maker in d.get("manufacturer", "").lower():
            names.append(re.sub(r"\s*\([^)]*\d{4}\)$", "", d["game"]))  # drop "(Maker, Year)"
    return sorted(set(names), key=len, reverse=True)


def _core(name: str) -> str:
    """Title key without a leading "The", "(SE)"-style tags, or a subtitle.

    "The Texas Chainsaw Massacre (SE)" -> "texaschainsawmassacre",
    "Ultraman: Kaiju Rumble (SE)" -> "ultraman".
    """
    name = re.sub(r"\([^)]*\)", "", html.unescape(name)).split(":", 1)[0]
    return _key(re.sub(r"^\s*the\s+", "", name, flags=re.IGNORECASE))


def _same_game(title: str, games: list[str]) -> str | None:
    """The game a page title names, comparing core names ("Scooby Doo Where Are You!"
    is the Kineticist game "Scooby Doo")."""
    t = _core(title)
    for g in games:
        c = _core(g)
        if len(c) >= 5 and len(t) >= 5 and (t.startswith(c) or c.startswith(t)):
            return g
    return None


def _match_game(text: str, games: list[str]) -> str | None:
    """Find a game named in text by full name, by its last words ("Labyrinth" for
    "Jim Henson's Labyrinth"), or by initials (e.g. 'WMH')."""
    k = _key(text)
    for g in games:
        if _key(g) and _key(g) in k:
            return g
    for g in games:
        words = re.findall(r"[A-Za-z0-9']+", g)
        for i in range(1, len(words)):
            tail = _key(" ".join(words[i:]))
            if len(tail) >= 6 and tail in k:
                return g
    words = set(re.findall(r"[A-Za-z]+", text))
    for g in games:
        initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", g)).upper()
        if len(initials) >= 3 and initials in words:
            return g
    return None


def _wp(site: str, kind: str, **params) -> list[dict]:
    """All items of a WordPress REST collection (pages/posts), following pagination."""
    items, page = [], 1
    while True:
        query = "&".join(f"{k}={v}" for k, v in {"per_page": 100, "page": page, **params}.items())
        r = _get(f"{site}/wp-json/wp/v2/{kind}?{query}")
        items.extend(r.json())
        if page >= int(r.headers.get("X-WP-TotalPages", 1)):
            return items
        page += 1


# --------------------------------------------------------------------------
# Per-maker discovery
# --------------------------------------------------------------------------

def multimorphic() -> list[dict]:
    links = _content_links("https://www.multimorphic.com/p3-pinball-platform/games/", r"^/[a-z0-9/-]+/$")
    links = [u for u in links if "/category/" not in u]
    return [r for r in (_page_record(u, "Multimorphic") for u in links) if r]


def spooky() -> list[dict]:
    site = "https://www.spookypinball.com"
    games = kineticist_games("spooky")
    records = []
    for p in _wp(site, "pages", _fields="link,title"):
        game = _same_game(_strip_html(p["title"]["rendered"]), games)
        if game:
            rec = _page_record(p["link"], "Spooky Pinball")
            if rec:
                rec["game"] = game
                if "/game-support/" in p["link"]:
                    rec["sections"] = {"Support and Code Update Notes": "\n".join(rec["sections"].values())}
                records.append(rec)
    return records


def pinball_brothers() -> list[dict]:
    site = "https://www.pinballbrothers.com"
    links = [p["link"] for p in _wp(site, "pages", _fields="link,slug") if p["slug"].endswith("-pinball")]
    return [r for r in (_page_record(u, "Pinball Brothers") for u in links) if r]


def dutch_pinball() -> list[dict]:
    xml = _get("https://dutchpinball.com/sitemap.xml").text
    links = [u for u in re.findall(r"<loc>([^<]+)</loc>", xml) if re.search(r"/games/[^/]+$", u)]
    return [r for r in (_page_record(u, "Dutch Pinball") for u in links) if r]


def turner() -> list[dict]:
    links = _content_links("https://turnerpinball.com/games/", r"^/games/[a-z0-9-]+/$")
    return [r for r in (_page_record(u, "Turner Pinball") for u in links) if r]


def american_pinball() -> list[dict]:
    site = "https://americanpinball.com"
    links: list[str] = []
    for listing in ("released", "retired", "coming-soon"):
        for u in _content_links(f"{site}/{listing}/", r"^/[a-z0-9-]+/?$"):
            u = u.rstrip("/") + "/"
            if u not in links and not u.endswith(f"/{listing}/"):
                links.append(u)
    records = []
    for u in links:
        rec = _page_record(u, "American Pinball")
        if not rec:
            continue
        records.append(rec)
        support = f"{site}/support/{urlsplit(u).path.strip('/')}/"
        try:
            notes = page_sections(_soup(support))
        except requests.HTTPError:
            continue
        if notes:
            records.append(_record(rec["game"], "American Pinball", support,
                                   {"Support and Code Update Notes": "\n".join(notes.values())}))
    return records


def barrels_of_fun() -> list[dict]:
    site = "https://www.barrelsoffun.com"
    games = kineticist_games("barrels of fun")
    notes: dict[str, list[tuple[str, str, str]]] = {}
    items = [p for p in _wp(site, "posts", _fields="link,title,date,content")
             if "code update" in _strip_html(p["title"]["rendered"]).lower()]
    items += [p for p in _wp(site, "pages", _fields="link,title,date,content")
              if "update history" in _strip_html(p["title"]["rendered"]).lower()]
    for p in items:
        title = _strip_html(p["title"]["rendered"])
        game = _match_game(title, games)
        if not game:
            print(f"  barrels_of_fun: no machine named in {title!r}, skipped", flush=True)
            continue
        body = BeautifulSoup(p["content"]["rendered"], "html.parser").get_text("\n")
        body = "\n".join(ln.strip() for ln in body.splitlines() if ln.strip())
        notes.setdefault(game, []).append((p["date"][:10], title, body))
    records = []
    for game, entries in notes.items():
        # One section per update, labeled with its date, so every retrieved chunk
        # says which update (and how recent) it is.
        entries.sort(reverse=True)  # newest first
        sections = {f"Code Update Notes, posted {date}: {title}": body for date, title, body in entries}
        records.append(_record(game, "Barrels of Fun", f"{site}/", sections))
    return records


MAKERS = {
    "multimorphic": multimorphic,
    "spooky": spooky,
    "pinball_brothers": pinball_brothers,
    "dutch_pinball": dutch_pinball,
    "turner": turner,
    "american_pinball": american_pinball,
    "barrels_of_fun": barrels_of_fun,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape smaller pinball manufacturers' sites.")
    parser.add_argument("--only", choices=sorted(MAKERS), action="append",
                        help="Only this maker (repeatable)")
    parser.add_argument("--out", type=Path, default=Path("data/raw"),
                        help="Parent directory; each maker writes to <out>/<maker>/")
    args = parser.parse_args()

    failed = 0
    for name in args.only or MAKERS:
        print(f"== {name}", flush=True)
        try:
            records = MAKERS[name]()
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr, flush=True)
            failed += 1
            continue
        if not records:
            # Never wipe a maker's existing files because of an empty or changed site.
            print("  nothing found; existing files left unchanged", flush=True)
            continue
        out = args.out / name
        out.mkdir(parents=True, exist_ok=True)
        files: dict[str, dict] = {}
        for rec in records:
            base = slugify(rec["game"])
            fname, n = f"{base}.json", 2
            while fname in files:  # e.g. a game page plus its support page
                fname, n = f"{base}_{n}.json", n + 1
            files[fname] = rec
        for old in out.glob("*.json"):
            if old.name not in files:
                old.unlink()
        for fname, rec in files.items():
            (out / fname).write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"  {rec['game']} ({len(rec['sections'])} sections)", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
