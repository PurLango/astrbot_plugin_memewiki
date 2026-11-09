import asyncio
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

import page_api
from main import MemeWikiPlugin
from page_api import MemeWikiPageApi, _api_item_to_row


def make_plugin(temp_dir: str) -> MemeWikiPlugin:
    plugin = object.__new__(MemeWikiPlugin)
    plugin._data_dir = Path(temp_dir)
    plugin._db_path = plugin._data_dir / "memes.db"
    plugin._db_write_lock = asyncio.Lock()
    with contextlib.closing(sqlite3.connect(plugin._db_path)) as conn:
        conn.execute(
            "CREATE TABLE memes ("
            "title TEXT PRIMARY KEY, slug TEXT, tags TEXT, usage TEXT, "
            "origin TEXT, other TEXT, summary TEXT, aliases TEXT)"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO meta VALUES ('built_at', '2026-11-05 12:48:06')"
        )
        conn.execute(
            "INSERT INTO memes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("已有词", "existing", "测试", "本地用法", "本地来源", "", "本地修改", "旧称"),
        )
        conn.commit()
    plugin._load_database_index()
    return plugin


class MemeWikiPageApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = make_plugin(self.temp.name)
        self.api = MemeWikiPageApi(self.plugin)
        page_api.request.args = {}
        page_api.request.payload = {}

    def tearDown(self):
        self.temp.cleanup()

    def test_parses_public_api_sections(self):
        row = _api_item_to_row(
            {
                "title": "测试梗",
                "slug": "test-meme",
                "tags": ["游戏", "网络"],
                "aliases": ["测试别名"],
                "summary": "一句话释义",
                "content_html": (
                    "<h2>来源</h2><p>来自某处</p>"
                    "<h2>用法</h2><ul><li>第一种用法</li><li>第二种用法</li></ul>"
                    "<h2>演化</h2><p>后来发生了变化</p>"
                ),
            }
        )

        self.assertEqual(row["origin"], "来自某处")
        self.assertIn("第一种用法", row["usage"])
        self.assertIn("演化", row["other"])
        self.assertEqual(row["tags"], "游戏、网络")
        self.assertEqual(row["aliases"], "测试别名")

    async def test_create_rename_search_and_delete_entry(self):
        page_api.request.payload = {
            "original_title": "",
            "entry": {"title": "新词", "summary": "新释义", "aliases": "新别名"},
        }
        created = await self.api.save_entry()
        self.assertEqual(created["status"], "ok")
        self.assertTrue(created["data"]["created"])
        self.assertEqual(self.plugin._database_entry("新别名")["title"], "新词")

        page_api.request.payload = {
            "original_title": "新词",
            "entry": {"title": "改名词", "summary": "修改后"},
        }
        renamed = await self.api.save_entry()
        self.assertEqual(renamed["data"]["entry"]["title"], "改名词")
        self.assertIsNone(self.plugin._database_entry("新词"))

        page_api.request.args = {"query": "改名", "page": "1", "page_size": "10"}
        listed = await self.api.entries()
        self.assertEqual(listed["data"]["total"], 1)

        page_api.request.payload = {"title": "改名词"}
        deleted = await self.api.delete_entry()
        self.assertTrue(deleted["data"]["deleted"])
        self.assertIsNone(self.plugin._database_entry("改名词"))

    async def test_sync_only_adds_missing_entries(self):
        fetched_urls = []
        responses = {
            f"{page_api.PUBLIC_API}?limit=30&offset=0": {
                "items": [
                    {
                        "title": "社区新词",
                        "slug": "community-new",
                        "updated_at": "2026-11-09T12:00:00+08:00",
                    },
                    {
                        "title": "已有词",
                        "slug": "existing",
                        "updated_at": "2026-11-04T12:00:00+08:00",
                    },
                ],
                "has_more": True,
            },
            f"{page_api.PUBLIC_API}/community-new": {
                "title": "社区新词",
                "slug": "community-new",
                "summary": "社区释义",
                "content_html": "<h2>来源</h2><p>社区来源</p>",
            },
        }

        async def fake_fetch(url, retries=4):
            fetched_urls.append(url)
            return responses[url]

        self.api._fetch_json = fake_fetch
        await self.api._run_sync()

        existing = self.plugin._database_entry("已有词")
        added = self.plugin._database_entry("社区新词")
        status = self.api._status_snapshot()
        self.assertEqual(existing["summary"], "本地修改")
        self.assertEqual(existing["usage"], "本地用法")
        self.assertEqual(added["summary"], "社区释义")
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["added"], 1)
        self.assertEqual(len(list((self.plugin._data_dir / "backups").glob("*.db"))), 1)
        self.assertEqual(
            fetched_urls,
            [
                f"{page_api.PUBLIC_API}?limit=30&offset=0",
                f"{page_api.PUBLIC_API}/community-new",
            ],
        )
        with contextlib.closing(sqlite3.connect(self.plugin._db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_remote_updated_at'"
            ).fetchone()[0]
        self.assertEqual(cursor, "2026-11-09T12:00:00+08:00")

        second_api = MemeWikiPageApi(self.plugin)
        second_fetches = []

        async def second_fetch(url, retries=4):
            second_fetches.append(url)
            return responses[f"{page_api.PUBLIC_API}?limit=30&offset=0"]

        second_api._fetch_json = second_fetch
        await second_api._run_sync()
        self.assertEqual(
            second_fetches, [f"{page_api.PUBLIC_API}?limit=30&offset=0"]
        )
        self.assertEqual(second_api._status_snapshot()["state"], "done")
        self.assertEqual(
            len(list((self.plugin._data_dir / "backups").glob("*.db"))), 1
        )


if __name__ == "__main__":
    unittest.main()
