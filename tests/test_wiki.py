from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from feishu_archive.config import ArchivePaths
from feishu_archive.database import ArchiveDatabase
from feishu_archive.wiki import WikiSyncer, block_text, render_blocks


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, *, filename: str, content_type: str) -> None:
        super().__init__(body)
        self.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{filename}"',
        }


class FakeWikiClient:
    def __init__(self) -> None:
        self.media_opens = 0
        self.file_opens = 0

    def iter_wiki_space_pages(self):
        yield {
            "items": [
                {
                    "space_id": "spc_1",
                    "name": "可持续发展知识库",
                    "description": "离线测试空间",
                    "space_type": "team",
                }
            ]
        }

    def iter_wiki_node_pages(self, space_id, *, parent_node_token=None):
        self.last_space_id = space_id
        if parent_node_token is None:
            yield {
                "items": [
                    {
                        "node_token": "wik_doc",
                        "obj_token": "doc_1",
                        "obj_type": "docx",
                        "title": "离线知识库方案",
                        "obj_edit_time": "1720000000",
                        "has_child": True,
                    },
                    {
                        "node_token": "wik_file",
                        "obj_token": "file_1",
                        "obj_type": "file",
                        "title": "证据清单.pdf",
                        "obj_edit_time": "1720000001",
                        "has_child": False,
                    },
                ]
            }
        elif parent_node_token == "wik_doc":
            yield {
                "items": [
                    {
                        "node_token": "wik_sheet",
                        "obj_token": "sheet_1",
                        "obj_type": "sheet",
                        "title": "指标表",
                        "obj_edit_time": "1720000002",
                        "has_child": False,
                    }
                ]
            }
        else:
            yield {"items": []}

    def get_docx_document(self, document_id):
        return {"document_id": document_id, "title": "离线知识库方案", "revision_id": 7}

    def iter_docx_block_pages(self, document_id):
        yield {
            "items": [
                {
                    "block_id": "root",
                    "block_type": 1,
                    "page": {"elements": "离线知识库方案"},
                },
                {
                    "block_id": "text_1",
                    "parent_id": "root",
                    "block_type": 2,
                    "text": {"elements": [{"text_run": {"content": "正文可以本机全文搜索"}}]},
                },
                {
                    "block_id": "image_1",
                    "parent_id": "root",
                    "block_type": 27,
                    "image": {"token": "img_1", "name": "架构图.png"},
                },
            ]
        }

    def get_docx_raw_content(self, document_id):
        return "离线知识库方案\n正文可以本机全文搜索"

    def open_drive_media(self, file_token):
        self.media_opens += 1
        return FakeResponse(b"fake-png", filename="architecture.png", content_type="image/png")

    def open_drive_file(self, file_token):
        self.file_opens += 1
        return FakeResponse(b"fake-pdf", filename="evidence.pdf", content_type="application/pdf")


class WikiSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.paths = ArchivePaths(Path(self.tempdir.name))
        self.paths.ensure()
        self.database = ArchiveDatabase(self.paths.database)
        self.database.initialize()
        self.client = FakeWikiClient()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_space_tree_docx_file_assets_and_incremental_sync(self) -> None:
        syncer = WikiSyncer(self.database, self.client, self.paths)
        counts, errors = syncer.sync()

        self.assertEqual(errors, [])
        self.assertEqual(counts.spaces_seen, 1)
        self.assertEqual(counts.nodes_seen, 3)
        self.assertEqual(counts.documents_written, 3)
        self.assertEqual(counts.assets_downloaded, 2)
        self.assertEqual(self.client.media_opens, 1)
        self.assertEqual(self.client.file_opens, 1)

        status = self.database.wiki_status()
        self.assertEqual(status["spaces"], 1)
        self.assertEqual(status["nodes"], 3)
        self.assertEqual(status["synced_documents"], 2)
        self.assertEqual(status["metadata_only_documents"], 1)
        matches = self.database.search_wiki_documents("全文搜索")
        self.assertEqual(matches[0]["node_token"], "wik_doc")

        document = self.database.wiki_document_for_node("wik_doc")
        self.assertIn("/api/wiki/assets/", document["rendered_html"])
        export_path = Path(document["local_export_path"])
        self.assertTrue(export_path.is_file())
        export_text = export_path.read_text(encoding="utf-8")
        self.assertIn("../assets/", export_text)
        self.assertNotIn("/api/wiki/assets/", export_text)
        for asset in self.database.list_wiki_assets("doc_1"):
            self.assertTrue(Path(asset["local_path"]).is_file())

        second_counts, second_errors = syncer.sync()
        self.assertEqual(second_errors, [])
        self.assertEqual(second_counts.documents_written, 0)
        self.assertEqual(self.client.media_opens, 1)
        self.assertEqual(self.client.file_opens, 1)

    def test_rendering_escapes_document_text(self) -> None:
        block = {
            "block_id": "text",
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": "<script>bad()</script>"}}]},
        }
        self.assertEqual(block_text(block), "<script>bad()</script>")
        rendered = render_blocks([block], {})
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)


if __name__ == "__main__":
    unittest.main()
