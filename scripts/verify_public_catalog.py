"""Validate the exact contents of the metadata-only GitHub repository."""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = {
    ".github/workflows/update-games.yml",
    "README.md",
    "catalog/games.json",
    "catalog/licenses/titledb.txt",
    "catalog/verified-games.json",
    "scripts/update_games.py",
    "scripts/verify_public_catalog.py",
    "tests/test_update_games.py",
}
PUBLIC_FIELDS = {
    "id", "platform", "format", "edition", "names", "regions", "nsuid", "titleId", "productCode",
    "origin", "releaseDate", "publisher", "cover", "icon", "aliases", "sourceUrl", "medium", "verified",
}


def verify_catalog(root=ROOT):
    indexed = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8").strip("\0").split("\0")
    if set(indexed) != PUBLIC_FILES:
        raise ValueError(f"Unexpected publication paths: {sorted(set(indexed) ^ PUBLIC_FILES)}")
    catalog = json.loads((root / "catalog" / "games.json").read_text(encoding="utf-8"))
    expected = {"schemaVersion", "generatedAt", "games"}
    if set(catalog) not in (expected, expected | {"summary"}) or catalog["schemaVersion"] != 1:
        raise ValueError("Unexpected publication schema")
    if "summary" in catalog:
        summary = catalog["summary"]
        if set(summary) != {"platforms", "sources", "recent"} or any(
            set(row) != {"id", "name", "url", "updated"} for row in summary["sources"]
        ) or any(set(row) != {"id", "releaseDate"} for row in summary["recent"]):
            raise ValueError("Unexpected public catalog summary")
    verified = json.loads((root / "catalog" / "verified-games.json").read_text(encoding="utf-8"))
    for rows in (catalog["games"], verified):
        if not isinstance(rows, list) or not rows:
            raise ValueError("An empty catalog cannot be published")
        for row in rows:
            if not isinstance(row, dict) or set(row) - PUBLIC_FIELDS:
                raise ValueError("Non-public fields found in catalog")
            if not row.get("id") or not row.get("names") or set(row["names"]) - {"zh", "en", "ja"}:
                raise ValueError("Invalid public game names")
            for key in ("cover", "icon", "sourceUrl"):
                if key in row and not row[key].startswith(("https://", "covers/")):
                    raise ValueError("Invalid public image or source reference")
    print(f"Verified {len(PUBLIC_FILES)} public files and {len(catalog['games']):,} records.")


if __name__ == "__main__":
    try:
        verify_catalog()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
