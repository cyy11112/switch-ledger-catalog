"""Generate the public catalog; this script never reads personal ledger files."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://raw.githubusercontent.com/blawar/titledb/master/"
TRACKER = "https://raw.githubusercontent.com/codemaverick-hub/switch2-tracker/main/data/games.json"
WIKIDATA = "https://query.wikidata.org/sparql"
WIKIDATA_SWITCH2 = """
SELECT ?game (SAMPLE(?en) AS ?nameEn) (SAMPLE(?zh) AS ?nameZh)
       (SAMPLE(?ja) AS ?nameJa) (MIN(?specific) AS ?released) WHERE {
  ?game wdt:P400 wd:Q122761124 .
  OPTIONAL { ?game rdfs:label ?en FILTER(LANG(?en) = "en") }
  OPTIONAL { ?game rdfs:label ?zh FILTER(LANG(?zh) = "zh") }
  OPTIONAL { ?game rdfs:label ?ja FILTER(LANG(?ja) = "ja") }
  OPTIONAL {
    ?game p:P577 ?releaseStatement .
    ?releaseStatement ps:P577 ?specific ; pq:P400 wd:Q122761124 ;
      psv:P577/wikibase:timePrecision 11 .
  }
}
GROUP BY ?game
"""
LOCALES = {"US.en": ("US", "en"), "HK.zh": ("HK", "zh"), "JP.ja": ("JP", "ja"), "CN.zh": ("CN", "zh")}
PLATFORMS = {"switch", "switch2", "unknown"}
FORMATS = {"card", "key-card", "digital", "dlc", "unknown"}
EDITIONS = {"standard", "switch2-edition", "upgrade-pack", "deluxe", "collectors", "other"}
MIB = 1024 * 1024
CATALOG_LIMIT = 48 * MIB
SWITCH2_NAME = re.compile(r"nintendo\s*switch\s*2\s*(?:edition|版)|switch\s*2\s*edition", re.I)
TITLE_ID = re.compile(r"(?:01|04)[0-9A-F]{14}$")
SKU = re.compile(r"700(?:100|500)\d{8}$")


def fetch_json(url, limit):
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    contact = f"https://github.com/{repository}/issues" if re.fullmatch(r"[\w.-]+/[\w.-]+", repository) else (
        "https://www.wikidata.org/wiki/Q122761124"
    )
    request = Request(url, headers={"User-Agent": f"SwitchLedger-Catalog/1.2 ({contact})",
                                   "Accept": "application/json, application/sparql-results+json"})
    with urlopen(request, timeout=60) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"Response exceeded the size limit: {url}")
    return json.loads(content.decode("utf-8-sig"))


def clean(value, limit=160):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def label_key(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text.lower().replace("™", "").replace("®", "")) if c.isalnum())


def release_date(value):
    match = re.fullmatch(r"(\d{4})-?(\d\d)-?(\d\d)", str(value or ""))
    if match:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            pass
    return ""


def image_url(value):
    if not isinstance(value, str) or len(value) > 600:
        return ""
    url = urlsplit(value)
    if url.scheme == "https" and url.hostname and not url.username and not url.password and not url.port:
        if url.hostname in {"img-eshop.cdn.nintendo.net", "assets.nintendo.com", "www.nintendo.com",
                            "raw.githubusercontent.com", "search.nintendo.jp"}:
            return value
    return ""


def title_rows(file_name, source_dir):
    region, locale = LOCALES[file_name]
    raw = (json.loads((source_dir / (file_name + ".json")).read_text(encoding="utf-8-sig"))
           if source_dir else fetch_json(BASE + file_name + ".json", 128 * MIB))
    if not isinstance(raw, dict) or len(raw) < (50 if region == "CN" else 4000):
        raise ValueError(f"Unexpectedly small TitleDB feed: {file_name}")
    rows = []
    for key, row in raw.items():
        if not isinstance(row, dict) or row.get("isDemo") or not SKU.fullmatch(key):
            continue
        title = clean(row.get("name"))
        if not title:
            continue
        code = str(row.get("id", "")).upper()
        code = code if TITLE_ID.fullmatch(code) else ""
        fmt = "digital" if key.startswith("700100") else "dlc"
        edition = "standard"
        if SWITCH2_NAME.search(unicodedata.normalize("NFKC", title.replace("™", "").replace("®", ""))):
            edition = "switch2-edition" if fmt == "digital" else "upgrade-pack"
        when = release_date(row.get("releaseDate"))
        platform = "unknown"
        if code.startswith("04") or edition != "standard":
            platform = "switch2"
        elif code.startswith("01") or (when and when < "2025-06-05"):
            platform = "switch"
        game = {"id": "tdb-" + key, "platform": platform, "format": fmt, "edition": edition,
                "names": {locale: title}, "regions": [region], "nsuid": key, "origin": "titledb"}
        for field, value in (("titleId", code), ("releaseDate", when), ("publisher", clean(row.get("publisher"))),
                             ("cover", image_url(row.get("frontBoxArt"))), ("icon", image_url(row.get("iconUrl")))):
            if value:
                game[field] = value
        rows.append(game)
    return rows


def titledb(source_dir):
    with ThreadPoolExecutor(max_workers=len(LOCALES)) as pool:
        pages = list(pool.map(lambda name: title_rows(name, source_dir), LOCALES))
    games = {}
    for row in (row for page in pages for row in page):
        game = games.get(row["id"])
        if game:
            if game.get("titleId") and row.get("titleId") and game["titleId"] != row["titleId"]:
                raise ValueError(f"Conflicting title IDs for {row['id']}")
            if row["platform"] == "switch2":
                game["platform"], game["edition"] = row["platform"], row["edition"]
            elif game["platform"] == "unknown":
                game["platform"] = row["platform"]
            game["regions"].extend(row["regions"])
            game["names"].update({k: v for k, v in row["names"].items() if k not in game["names"]})
            for key in ("cover", "icon", "publisher", "titleId", "releaseDate"):
                if not game.get(key) and row.get(key):
                    game[key] = row[key]
        else:
            game = row
        games[game["id"]] = game
    by_title = defaultdict(list)
    for row in games.values():
        if row.get("titleId"):
            by_title[(row["titleId"], row["platform"], row["format"], row["edition"])].append(row)
    # Alternate names come only from one unambiguous Title ID + platform + format + edition group.
    for siblings in by_title.values():
        names = defaultdict(set)
        for row in siblings:
            for locale, title in row["names"].items():
                names[locale].add(title)
        for row in siblings:
            game = games[row["id"]]
            for locale, titles in names.items():
                if locale not in game["names"] and len(titles) == 1:
                    game["names"][locale] = next(iter(titles))
    return games


def reviewed_rows(path, base):
    rows = json.loads(path.read_text(encoding="utf-8"))
    reviewed = {}
    for row in rows:
        game = dict(row)
        supplement = base.get("tdb-" + game.get("nsuid", ""))
        if supplement:
            if supplement["platform"] not in ("unknown", game["platform"]):
                raise ValueError(f"Reviewed platform conflicts with linked eShop SKU: {game['id']}")
            game["names"] = {**supplement["names"], **game["names"]}
            # The digital SKU refers to the same title, not to a retail product ID.
            for key in ("titleId", "icon"):
                if supplement.get(key) and not game.get(key):
                    game[key] = supplement[key]
            if game["format"] == supplement["format"] and game["edition"] == supplement["edition"]:
                game["id"] = supplement["id"]
                game["regions"] = sorted(set(game["regions"] + supplement["regions"]))
                base.pop(game["id"])
        for key in ("cover", "icon"):
            if key in game and not (image_url(game[key]) or re.fullmatch(r"covers/[a-z0-9-]+\.webp", game[key])):
                raise ValueError(f"Invalid artwork reference: {game['id']}")
        if game["id"] in base or game["id"] in reviewed:
            raise ValueError(f"Duplicate catalog ID {game['id']}")
        reviewed[game["id"]] = game
    return reviewed


def report_tracker(path, reviewed):
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig")) if path else fetch_json(TRACKER, 4 * MIB)
        names = {label_key(game["names"].get("en", "")) for game in reviewed.values()}
        count = sum(1 for row in payload["games"] if isinstance(row.get("title"), str) and label_key(row["title"]) not in names)
        print(f"Switch 2 Tracker discovery: {count} titles to review against publisher sources; no tracker rows published.")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Switch 2 Tracker discovery unavailable: {error}", file=sys.stderr)


def wikidata_rows(payload, existing, previous):
    bindings = payload["results"]["bindings"]
    if not isinstance(bindings, list) or len(bindings) < 100:
        raise ValueError("Wikidata returned an incomplete Switch 2 list.")
    saved = {row["id"]: row for row in previous if row.get("origin") == "wikidata"}
    existing_names = {
        label_key(name) for game in existing.values() if game["platform"] == "switch2"
        for name in game["names"].values()
    }
    for binding in bindings:
        qid = binding.get("game", {}).get("value", "").rsplit("/", 1)[-1]
        if not re.fullmatch(r"Q[1-9]\d*", qid):
            continue
        row_id = "wiki-" + qid.lower()
        names = {locale: clean(binding.get(key, {}).get("value"))
                 for locale, key in (("en", "nameEn"), ("zh", "nameZh"), ("ja", "nameJa"))}
        names = {lang: name for lang, name in names.items() if name and name != qid}
        if not names or (row_id not in saved and any(label_key(name) in existing_names for name in names.values())):
            continue
        # A platform claim identifies the work. It does not identify a cartridge, region, or eShop SKU.
        row = {"id": row_id, "platform": "switch2", "format": "unknown", "edition": "standard",
               "names": names, "regions": ["ZZ"], "origin": "wikidata",
               "sourceUrl": f"https://www.wikidata.org/wiki/{qid}"}
        if any(SWITCH2_NAME.search(unicodedata.normalize("NFKC", name.replace("™", "").replace("®", "")))
               for name in names.values()):
            row["edition"] = "switch2-edition"
        when = release_date(binding.get("released", {}).get("value", "")[:10])
        if when:
            row["releaseDate"] = when
        else:
            row["releaseDate"] = saved.get(row_id, {}).get("releaseDate", "")
            if not row["releaseDate"]:
                del row["releaseDate"]
        saved[row_id] = row
    return saved


def discover_switch2(source_file, games, previous):
    try:
        query_url = WIKIDATA + "?" + urlencode({"query": WIKIDATA_SWITCH2, "format": "json"})
        payload = json.loads(source_file.read_text(encoding="utf-8-sig")) if source_file else fetch_json(query_url, 8 * MIB)
        return wikidata_rows(payload, games, previous)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"Wikidata unavailable, keeping previous records: {error}", file=sys.stderr)
        return {row["id"]: row for row in previous if row.get("origin") == "wikidata"}


def commit_snapshot(output, games):
    previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
    rows = sorted(games.values(), key=lambda row: row["id"])
    ids = {row["id"] for row in rows}
    if len(ids) != len(rows) or not rows:
        raise ValueError("Catalog is empty or has duplicate IDs")
    for row in rows:
        if (row.get("platform") not in PLATFORMS or row.get("format") not in FORMATS
                or row.get("edition") not in EDITIONS or not row.get("names") or not row.get("regions")):
            raise ValueError(f"Incomplete catalog row: {row['id']}")
    if previous:
        overlap = sum(row["id"] in ids for row in previous["games"])
        if overlap < .92 * len(previous["games"]):
            raise ValueError("Source lost more than 8% of the previous catalog; previous snapshot kept.")
        if rows == previous["games"]:
            return False
    document = {"schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "games": rows}
    content = (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(content) > CATALOG_LIMIT:
        raise ValueError("Catalog exceeds the app's 48 MiB feed limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        try:
            temporary.write(content)
            temporary.close()
            os.replace(temporary.name, output)
        finally:
            Path(temporary.name).unlink(missing_ok=True)
    return True


def report_changes(previous, games, written):
    baseline = {row["id"]: row for row in previous}
    added = [row for key, row in games.items() if key not in baseline]
    changed = sum(old_id in games and row != games[old_id] for old_id, row in baseline.items())
    removed = sum(old_id not in games for old_id in baseline)
    switch2 = [row for row in added if row["platform"] == "switch2"]
    summary = [
        "## Catalog update",
        "",
        f"- Rows: {len(games):,}",
        f"- Added: {len(added):,} ({len(switch2):,} Switch 2)",
        f"- Changed: {changed:,}",
        f"- No longer in source: {removed:,}",
        f"- Snapshot: {'updated' if written else 'unchanged'}",
    ]
    if added:
        summary.append(f"- New records by source: {dict(Counter(row['origin'] for row in added))}")
    if switch2:
        summary.extend(["", "Examples of new Switch 2 records", ""])
        for row in sorted(switch2, key=lambda row: row.get("releaseDate", ""), reverse=True)[:12]:
            names = row["names"]
            title = names.get("en") or names.get("zh") or names.get("ja")
            summary.append(f"- {title} ({row['id']}) {row.get('releaseDate', '')}".rstrip())
    result = "\n".join(summary) + "\n"
    print(result, end="")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as report:
            report.write(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, help="Use already downloaded TitleDB JSON for a reproducible build")
    parser.add_argument("--tracker-file", type=Path, help="Previously downloaded tracker feed for discovery checks")
    parser.add_argument("--skip-tracker", action="store_true")
    parser.add_argument("--wikidata-file", type=Path, help="Use a captured Wikidata Query Service response")
    parser.add_argument("--verified", type=Path, default=ROOT / "catalog" / "verified-games.json")
    parser.add_argument("--output", type=Path, default=ROOT / "catalog" / "games.json")
    args = parser.parse_args()
    previous = json.loads(args.output.read_text(encoding="utf-8"))["games"] if args.output.exists() else []
    base = titledb(args.source_dir)
    reviewed = reviewed_rows(args.verified, base)
    if not args.skip_tracker:
        report_tracker(args.tracker_file, reviewed)
    games = {**base, **reviewed}
    games.update(discover_switch2(args.wikidata_file, games, previous))
    report_changes(previous, games, commit_snapshot(args.output, games))


if __name__ == "__main__":
    main()
