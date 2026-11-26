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
FORK = "https://raw.githubusercontent.com/borntohonk/titledb/master/"
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
FORK_LOCALES = {**LOCALES, "SG.en": ("SG", "en")}
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


def title_rows(file_name, source_dir, fork=False):
    region, locale = FORK_LOCALES[file_name]
    prefix = "fork-" if fork else ""
    raw = (json.loads((source_dir / (prefix + file_name + ".json")).read_text(encoding="utf-8-sig"))
           if source_dir else fetch_json((FORK if fork else BASE) + file_name + ".json", 128 * MIB))
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
                "names": {locale: title}, "regions": [region], "nsuid": key,
                "origin": "titledb-fork" if fork else "titledb"}
        for field, value in (("titleId", code), ("releaseDate", when), ("publisher", clean(row.get("publisher"))),
                             ("cover", image_url(row.get("frontBoxArt"))), ("icon", image_url(row.get("iconUrl")))):
            if value:
                game[field] = value
        rows.append(game)
    return rows


def titledb(source_dir, fork=False):
    locales = FORK_LOCALES if fork else LOCALES
    with ThreadPoolExecutor(max_workers=3) as pool:
        pages = list(pool.map(lambda name: title_rows(name, source_dir, fork), locales))
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
        if "medium" in game and (game["medium"] != "boxed-code" or game["format"] != "unknown"):
            raise ValueError(f"Invalid retail medium: {game['id']}")
        if game.get("verified") is True:
            if game.get("format") not in ("card", "key-card") and game.get("medium") != "boxed-code":
                raise ValueError(f"Verified retail release has no retail medium: {game['id']}")
            source = urlsplit(game.get("sourceUrl", ""))
            if source.scheme != "https" or not source.hostname or game.get("regions") in (None, [], ["ZZ"]):
                raise ValueError(f"Verified retail release lacks regional evidence: {game['id']}")
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


def report_tracker(path, catalog):
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig")) if path else fetch_json(TRACKER, 4 * MIB)
        names = {work_name(name) for game in catalog.values()
                 if game["platform"] == "switch2" and game["format"] != "dlc" for name in game["names"].values()}
        candidates = {work_name(row["title"]) for row in payload["games"]
                      if isinstance(row.get("title"), str) and row.get("status") == "r"}
        print(f"Switch 2 Tracker name check: {len(names & candidates):,} / {len(candidates):,} works marked released matched.")
        retail = defaultdict(set)
        for row in payload["games"]:
            if row.get("status") == "r" and isinstance(row.get("title"), str):
                formats = row.get("formats") or {}
                for code in set(formats.values()) | {row.get("fmt")}:
                    if code in ("c", "k", "b"):
                        retail[code].add(work_name(row["title"]))
        print(f"Switch 2 Tracker retail leads (review required): {len(retail['c']):,} card, "
              f"{len(retail['k']):,} key card, {len(retail['b']):,} boxed code.")
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


def merge_digital(previous, latest):
    base = {row["id"]: row for row in previous if row.get("origin") in {"titledb", "titledb-fork"}}
    conflicts = 0
    for key, row in latest.items():
        older = base.get(key)
        if older:
            if older.get("titleId") and row.get("titleId") and older["titleId"] != row["titleId"]:
                conflicts += 1
                continue
            row = {**older, **row, "names": {**older["names"], **row["names"]},
                   "regions": sorted(set(older["regions"] + row["regions"]))}
            if row["platform"] == "unknown":
                row["platform"], row["edition"] = older["platform"], older["edition"]
        base[key] = row
    if conflicts:
        print(f"Retained {conflicts} established SKUs with conflicting upstream Title IDs.", file=sys.stderr)
    return base


def work_name(name):
    text = unicodedata.normalize("NFKC", name.replace("™", "").replace("®", ""))
    text = re.split(r"nintendo\s*switch\s*2\s*edition", text, maxsplit=1, flags=re.I)[0]
    return label_key(text)


def work_groups(rows):
    """Estimate distinct games without changing catalog IDs or merging their releases."""
    rows = [row for row in rows if row["format"] != "dlc" and row["edition"] != "upgrade-pack"]
    roots = list(range(len(rows)))
    names = {}

    def root(index):
        while roots[index] != index:
            roots[index] = roots[roots[index]]
            index = roots[index]
        return index

    for i, row in enumerate(rows):
        keys = [("name", work_name(n)) for n in row["names"].values()]
        if row.get("titleId"):
            keys.append(("title", row["titleId"]))
        for key in keys:
            if len(key[1]) < 2:
                continue
            linked = names.setdefault(key, i)
            roots[root(i)] = root(linked)
    result = defaultdict(list)
    for i, row in enumerate(rows):
        result[root(i)].append(row)
    return list(result.values())


def platform_stats(rows):
    output = {}
    for platform in ("switch", "switch2"):
        platform_rows = [row for row in rows if row["platform"] == platform]
        physical = [row for row in platform_rows if row.get("origin") == "reviewed" and row.get("verified") is not False
                    and row.get("medium", row["format"]) in ("card", "key-card", "boxed-code")]
        counts = Counter(row.get("medium", row["format"]) for row in physical)
        groups = work_groups(platform_rows)
        cartridge_ids = {row["id"] for row in physical if row["format"] in ("card", "key-card")}
        output[platform] = {"works": len(groups), "retail": len(physical),
                            "retailWorks": sum(any(row["id"] in cartridge_ids for row in group) for group in groups),
                            "card": counts["card"], "keyCard": counts["key-card"], "boxedCode": counts["boxed-code"],
                            "covers": sum(bool(row.get("cover")) for row in physical)}
    return output


def source_info(previous):
    old = {row["id"]: row for row in previous}
    definitions = [
        ("titledb-fork", "TitleDB 活跃分支", "https://github.com/borntohonk/titledb",
         "borntohonk/titledb", "US.en.json"),
        ("titledb", "TitleDB 历史底库", "https://github.com/blawar/titledb",
         "blawar/titledb", "US.en.json"),
        ("tracker", "Switch 2 Tracker 核查", "https://github.com/codemaverick-hub/switch2-tracker",
         "codemaverick-hub/switch2-tracker", "data/games.json"),
        ("scrapper", "eShop 地区交叉核查", "https://github.com/dev-chin/eshopScrapper",
         "dev-chin/eshopScrapper", "output/main_regions_alt.json"),
        ("wikidata", "Wikidata 平台资料", "https://www.wikidata.org/wiki/Q122761124", "", ""),
        ("reviewed", "任天堂产品资料核实", "https://www.nintendo.com/zh-hans-hk/games/switch2/lineup", "", ""),
        ("publisher-retail", "发行商实体商品资料", "https://www.cyberpunk.net/en/news/51566/cyberpunk-2077-ultimate-edition-is-out-on-nintendo-switch-2", "", ""),
        ("retailer-retail", "零售商品规格核实", "https://www.bestbuy.com/product/split-fiction-standard-edition-nintendo-switch-2/6631377", "", ""),
    ]
    items = []
    for key, name, url, repo, path in definitions:
        updated = old.get(key, {}).get("updated", "")
        if repo:
            try:
                api = "https://api.github.com/repos/" + repo + "/commits?" + urlencode({"path": path, "per_page": 1})
                updated = fetch_json(api, 1 * MIB)[0]["commit"]["committer"]["date"]
            except (OSError, ValueError, IndexError, KeyError, TypeError):
                pass
        items.append({"id": key, "name": name, "url": url, "updated": updated})
    return items


def recent_records(previous, games):
    old = {row["id"] for row in previous.get("games", [])}
    dated = [row for row in games.values() if row["platform"] == "switch2" and row.get("releaseDate") and row["id"] not in old]
    changed = []
    for group in work_groups(dated):
        row = min(group, key=lambda row: (row["format"] == "unknown", row["id"]))
        changed.append({"id": row["id"], "releaseDate": row["releaseDate"]})
    today = date.today()
    changed.sort(key=lambda row: (date.fromisoformat(row["releaseDate"]) > today,
                                  abs((date.fromisoformat(row["releaseDate"]) - today).days), row["id"]))
    earlier = previous.get("summary", {}).get("recent", [])
    return list({row["id"]: row for row in changed + [row for row in earlier if row["id"] in games]}.values())[:25]


def commit_snapshot(output, games, summary=None):
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
        if rows == previous["games"] and (summary is None or summary == previous.get("summary")):
            return False
    document = {"schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "games": rows}
    if summary is not None:
        document["summary"] = summary
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
    known_switch2 = [row for row in previous if row["platform"] == "switch2"]
    originals = {row["id"] for row in known_switch2}
    new_works = [group for group in work_groups(known_switch2 + switch2)
                 if not any(row["id"] in originals for row in group)]
    summary = [
        "## Catalog update",
        "",
        f"- Rows: {len(games):,}",
        f"- Added: {len(added):,} ({len(switch2):,} Switch 2)",
        f"- Newly represented Switch 2 works (estimated, DLC excluded): {len(new_works):,}",
        f"- Changed: {changed:,}",
        f"- No longer in source: {removed:,}",
        f"- Snapshot: {'updated' if written else 'unchanged'}",
    ]
    if added:
        summary.append(f"- New records by source: {dict(Counter(row['origin'] for row in added))}")
    if new_works:
        summary.extend(["", "Examples of new Switch 2 records", ""])
        examples = [max(group, key=lambda row: row.get("releaseDate", "")) for group in new_works]
        today = date.today().isoformat()
        released = sorted((row for row in examples if row.get("releaseDate", "") <= today),
                          key=lambda row: row.get("releaseDate", ""), reverse=True)
        for row in (released or examples)[:12]:
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
    parser.add_argument("--fork-dir", type=Path, help="Use captured fork-REGION.json inputs for a reproducible build")
    parser.add_argument("--skip-source-revisions", action="store_true", help="Keep already recorded source revision dates")
    parser.add_argument("--tracker-file", type=Path, help="Previously downloaded tracker feed for discovery checks")
    parser.add_argument("--skip-tracker", action="store_true")
    parser.add_argument("--wikidata-file", type=Path, help="Use a captured Wikidata Query Service response")
    parser.add_argument("--verified", type=Path, default=ROOT / "catalog" / "verified-games.json")
    parser.add_argument("--output", type=Path, default=ROOT / "catalog" / "games.json")
    args = parser.parse_args()
    previous_document = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {}
    previous = previous_document.get("games", [])
    try:
        latest = titledb(args.fork_dir, fork=True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        if not previous:
            latest = titledb(args.source_dir)
        else:
            latest = {}
            print(f"TitleDB fork unavailable; retained last successful digital records: {error}", file=sys.stderr)
    base = merge_digital(previous, latest)
    reviewed = reviewed_rows(args.verified, base)
    games = {**{row["id"]: row for row in previous}, **base, **reviewed}
    games.update(discover_switch2(args.wikidata_file, games, previous))
    if not args.skip_tracker:
        report_tracker(args.tracker_file, games)
    old_sources = previous_document.get("summary", {}).get("sources", [])
    summary = {
        "platforms": platform_stats(list(games.values())),
        "sources": old_sources if args.skip_source_revisions else source_info(old_sources),
        "recent": recent_records(previous_document, games),
    }
    report_changes(previous, games, commit_snapshot(args.output, games, summary))


if __name__ == "__main__":
    main()
