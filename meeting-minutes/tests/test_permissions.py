import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.auth import Identity
from app.http_redirect import app as redirect_app


@pytest.mark.auth_boundary
def test_meeting_access_is_enforced_on_list_detail_export_and_mutations():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        with mock.patch.multiple(
            db, DATA_DIR=root, UPLOAD_DIR=root / "uploads",
            EXPORT_DIR=root / "exports", DB_PATH=root / "meetings.db",
            _local=threading.local(),
        ):
            db.init_db()
            own_id = db.create_meeting("甲的会议", "2026-09-27", "", [], "user-a")
            other_id = db.create_meeting("乙的会议", "2026-09-27", "", [], "user-b")
            legacy_id = db.create_meeting("历史会议", "2026-09-27", "", [])

            async def identity(token):
                return {
                    "a": Identity("user-a", "甲", False),
                    "b": Identity("user-b", "乙", False),
                    "admin": Identity("admin", "管理员", True),
                }.get(token)

            with mock.patch.object(main, "resolve_identity", side_effect=identity):
                client = TestClient(main.app)
                assert client.get("/api/meetings").status_code == 401
                assert client.get("/", follow_redirects=False).headers["location"] == main.LOGIN_URL
                assert client.get("/api/meetings", headers={"Authorization": "Bearer a"}).json()[0]["id"] == own_id
                assert len(client.get("/api/meetings", headers={"Authorization": "Bearer a"}).json()) == 1
                client.cookies.set("dl_auth_token", "b")
                assert client.get("/api/meetings").json()[0]["id"] == other_id
                client.cookies.clear()
                for mid in (other_id, legacy_id):
                    url = f"/api/meetings/{mid}"
                    headers = {"Authorization": "Bearer a"}
                    assert client.get(url, headers=headers).status_code == 404
                    assert client.get(url + "/records/1/docx", headers=headers).status_code == 404
                    assert client.delete(url, headers={**headers, "X-Meeting-Minutes-Action": "confirm"}).status_code == 404
                assert len(client.get("/api/meetings", headers={"Authorization": "Bearer admin"}).json()) == 3
                assert client.get(f"/api/meetings/{legacy_id}", headers={"Authorization": "Bearer admin"}).status_code == 200
                created = client.post(
                    "/api/meetings", json={"title": "新会议"},
                    headers={"Authorization": "Bearer a", "X-Meeting-Minutes-Action": "confirm"},
                )
                assert created.status_code == 201
                assert db.meeting_owner(created.json()["id"]) == "user-a"


def test_schema_upgrade_keeps_existing_meeting_unassigned():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        with mock.patch.multiple(
            db, DATA_DIR=root, UPLOAD_DIR=root / "uploads",
            EXPORT_DIR=root / "exports", DB_PATH=root / "meetings.db",
            _local=threading.local(),
        ):
            db.init_db()
            mid = db.create_meeting("升级前会议", "2026-09-01", "", [])
            conn = db.get_conn()
            conn.execute("DROP INDEX idx_meetings_owner")
            conn.execute("ALTER TABLE meetings DROP COLUMN owner_user_id")
            conn.execute("PRAGMA user_version=5")
            conn.commit()
            db.init_db()
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
            assert db.get_meeting(mid)["title"] == "升级前会议"
            assert db.meeting_owner(mid) is None


def test_old_http_entry_only_redirects_reads():
    client = TestClient(redirect_app)
    response = client.get("/api/meetings?x=1", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://192.168.100.179:8766/api/meetings?x=1"
    assert client.post("/api/meetings").status_code == 403
