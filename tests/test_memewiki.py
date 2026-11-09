import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from main import MemeWikiPlugin


class FakeEvent:
    def __init__(
        self,
        message: str,
        *,
        group_id: str = "100",
        user_id: str = "123",
        name: str = "测试用户",
        admin: bool = False,
    ):
        self.message_str = message
        self._group_id = group_id
        self._user_id = user_id
        self._name = name
        self._admin = admin

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._user_id

    def get_sender_name(self):
        return self._name

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        return text


def build_plugin(temp_dir: str) -> MemeWikiPlugin:
    plugin = object.__new__(MemeWikiPlugin)
    plugin.config = {}
    plugin._db_path = Path(__file__).resolve().parents[1] / "data" / "memes.db"
    plugin._data_dir = Path(temp_dir)
    plugin._terms_path = plugin._data_dir / "custom_terms.json"
    plugin._terms_lock = asyncio.Lock()
    plugin._custom_terms = {}
    plugin._load_database_index()
    return plugin


class MemeWikiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = build_plugin(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_bundled_database_has_expected_snapshot(self):
        entry = self.plugin._database_entry("114514")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["title"], "114514")

    def test_extracts_question_and_bare_term_without_matching_chat(self):
        self.assertEqual(
            self.plugin._extract_prefetch_query("去码头整点薯条是什么梗"),
            "去码头整点薯条",
        )
        self.assertEqual(self.plugin._extract_prefetch_query("114514"), "114514")
        self.assertIsNone(self.plugin._extract_prefetch_query("哈哈哈"))
        self.assertIsNone(self.plugin._extract_prefetch_query("你今天吃饭了吗"))

    async def test_lookup_command_returns_formatted_entry(self):
        reply = await anext(self.plugin.meme_lookup(FakeEvent("/梗 114514")))
        self.assertTrue(reply.startswith("【梗百科】\n词条：114514"))
        self.assertIn("释义：", reply)

    async def test_custom_slang_is_scoped_to_group_and_persisted(self):
        event = FakeEvent("/黑话 测试黑话 这是本群的特殊说法")
        reply = await anext(self.plugin.custom_slang(event))
        self.assertIn("已新增群内黑话", reply)

        entry = self.plugin._search_sync("测试黑话", "group:100")
        other_group = self.plugin._search_sync("测试黑话", "group:200")
        saved = json.loads(self.plugin._terms_path.read_text(encoding="utf-8"))

        self.assertEqual(entry["summary"], "这是本群的特殊说法")
        self.assertIsNone(other_group)
        self.assertIn("测试黑话", saved["scopes"]["group:100"])

    async def test_only_author_or_admin_can_update_custom_slang(self):
        await anext(
            self.plugin.custom_slang(
                FakeEvent("/黑话 测试黑话 初始释义", user_id="123")
            )
        )
        denied = await anext(
            self.plugin.custom_slang(
                FakeEvent("/黑话 测试黑话 错误覆盖", user_id="456")
            )
        )
        allowed = await anext(
            self.plugin.custom_slang(
                FakeEvent("/黑话 测试黑话 管理员修改", user_id="456", admin=True)
            )
        )

        self.assertIn("只有原记录者或管理员", denied)
        self.assertIn("已更新", allowed)
        self.assertEqual(
            self.plugin._search_sync("测试黑话", "group:100")["summary"],
            "管理员修改",
        )

    async def test_llm_tool_does_not_invent_missing_entry(self):
        reply = await self.plugin.search_meme_wiki(
            FakeEvent("陌生词"), "一个肯定不存在的测试黑话xyz"
        )
        self.assertIn("暂未收录", reply)
        self.assertIn("不要编造", reply)


if __name__ == "__main__":
    unittest.main()
