import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_games


class CatalogBuildTests(unittest.TestCase):
    def test_switch2_edition_and_upgrade_keep_distinct_skus(self):
        base = {
            "70010000000001": {"name": "Demo World", "id": "010000000000000A", "releaseDate": 20170303},
            "70010000000002": {"name": "Demo World Nintendo Switch™ 2 Edition", "id": "010000000000000A"},
            "70050000000003": {"name": "Demo World Nintendo Switch 2 Edition Upgrade Pack"},
            "70010000000004": {"name": "Demo World Trial", "isDemo": True},
        }
        source = {**{f"placeholder-{n}": {} for n in range(4000)}, **base}
        with patch.object(update_games, "fetch_json", return_value=source):
            rows = update_games.title_rows("US.en", None)
        self.assertEqual(
            [(row["id"], row["platform"], row["format"], row["edition"]) for row in rows],
            [("tdb-70010000000001", "switch", "digital", "standard"),
             ("tdb-70010000000002", "switch2", "digital", "switch2-edition"),
             ("tdb-70050000000003", "switch2", "dlc", "upgrade-pack")],
        )

    def test_reject_large_catalog_loss_without_replacing_previous_file(self):
        games = {
            f"record-{n}": {"id": f"record-{n}", "platform": "switch", "format": "digital", "edition": "standard",
                            "names": {"en": f"Fixture Game {n}"}, "regions": ["US"]}
            for n in range(20)
        }
        with TemporaryDirectory() as directory:
            output = Path(directory) / "games.json"
            update_games.commit_snapshot(output, games)
            previous = output.read_bytes()
            with self.assertRaisesRegex(ValueError, "previous snapshot kept"):
                update_games.commit_snapshot(output, dict(list(games.items())[:10]))
            self.assertEqual(output.read_bytes(), previous)
            update_games.commit_snapshot(output, games)
            self.assertEqual(output.read_bytes(), previous)
            self.assertEqual(len(json.loads(previous)["games"]), 20)

    def test_reject_incomplete_upstream_payload(self):
        with patch.object(update_games, "fetch_json", return_value={"partial": True}):
            with self.assertRaisesRegex(ValueError, "Unexpectedly small"):
                update_games.title_rows("JP.ja", None)

    def test_live_source_adds_a_new_work_without_inventing_a_card_or_sku(self):
        candidates = [{"game": {"value": f"http://www.wikidata.org/entity/Q{n}"}}
                      for n in range(1, 101)]
        candidates.extend([
            {"game": {"value": "http://www.wikidata.org/entity/Q101"}, "nameEn": {"value": "New Switch 2 Game"},
             "released": {"value": "2026-11-12T00:00:00Z"}},
            {"game": {"value": "http://www.wikidata.org/entity/Q102"}, "nameEn": {"value": "Known Game™"}},
        ])
        payload = {"results": {"bindings": candidates}}
        base = {"known": {"id": "known", "platform": "switch2", "names": {"en": "Known Game"}}}
        added = update_games.wikidata_rows(payload, base, [])
        self.assertEqual(list(added), ["wiki-q101"])
        self.assertEqual(added["wiki-q101"]["format"], "unknown")
        self.assertEqual(added["wiki-q101"]["releaseDate"], "2026-11-12")
        self.assertNotIn("nsuid", added["wiki-q101"])
        with patch.object(update_games, "fetch_json", side_effect=OSError("offline")):
            self.assertEqual(update_games.discover_switch2(None, base, list(added.values())), added)

    def test_active_fork_updates_languages_without_discarding_established_ids(self):
        old = {"id": "tdb-old", "platform": "switch2", "format": "digital", "edition": "standard",
               "names": {"en": "Test Game"}, "regions": ["US"], "titleId": "0400245021D2C000", "origin": "titledb"}
        new = {**old, "names": {"zh": "測試遊戲"}, "regions": ["HK"], "origin": "titledb-fork"}
        stale = {**old, "id": "tdb-stale"}
        result = update_games.merge_digital([old, stale], {"tdb-old": new})
        self.assertEqual(set(result), {"tdb-old", "tdb-stale"})
        self.assertEqual(result["tdb-old"]["regions"], ["HK", "US"])
        self.assertEqual(result["tdb-old"]["names"], {"en": "Test Game", "zh": "測試遊戲"})
        conflict = {**new, "titleId": "0400123456780000"}
        self.assertEqual(update_games.merge_digital([old], {"tdb-old": conflict})["tdb-old"], old)

    def test_independent_count_excludes_dlc_and_collapses_regional_skus(self):
        game = {"id": "digital-us", "platform": "switch2", "format": "digital", "edition": "standard",
                "names": {"en": "The Game"}, "titleId": "0400245021D2C000"}
        duplicate = {**game, "id": "digital-hk", "names": {"zh": "這個遊戲"}}
        card = {**game, "id": "boxed-hk", "format": "card", "cover": "covers/test.webp"}
        dlc = {**game, "id": "dlc", "format": "dlc", "names": {"en": "Test Season Pass"}}
        self.assertEqual(update_games.platform_stats([game, duplicate, card, dlc])["switch2"],
                         {"works": 1, "retail": 1, "covers": 1})


if __name__ == "__main__":
    unittest.main()
