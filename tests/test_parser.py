import json
import unittest

from feishu_archive.parser import normalize_message, parse_content


class ParserTests(unittest.TestCase):
    def test_text_and_image_resources(self) -> None:
        text, resources = parse_content("text", json.dumps({"text": "你好，离线档案"}))
        self.assertEqual(text, "你好，离线档案")
        self.assertEqual(resources, [])

        text, resources = parse_content("image", json.dumps({"image_key": "img_1"}))
        self.assertEqual(text, "[图片]")
        self.assertEqual(resources[0].file_key, "img_1")
        self.assertEqual(resources[0].resource_type, "image")

    def test_rich_text_is_flattened_for_search(self) -> None:
        payload = {
            "title": "项目更新",
            "content": [[{"tag": "text", "text": "第一阶段完成"}, {"tag": "at", "user_name": "王小明"}]],
        }
        text, _ = parse_content("post", payload)
        self.assertIn("项目更新", text)
        self.assertIn("第一阶段完成", text)
        self.assertIn("王小明", text)

    def test_normalize_message_preserves_thread_and_status(self) -> None:
        normalized = normalize_message(
            {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "thread_id": "omt_1",
                "msg_type": "text",
                "create_time": "1720000000000",
                "update_time": "1720000001000",
                "recalled": True,
                "sender": {"id": "ou_1", "sender_type": "user", "name": "测试用户"},
                "body": {"content": json.dumps({"text": "可搜索内容"}, ensure_ascii=False)},
            },
            "fallback",
        )
        self.assertEqual(normalized["chat_id"], "oc_1")
        self.assertEqual(normalized["thread_id"], "omt_1")
        self.assertTrue(normalized["recalled"])
        self.assertEqual(normalized["body_text"], "可搜索内容")


if __name__ == "__main__":
    unittest.main()
