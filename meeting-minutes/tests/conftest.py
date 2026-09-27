from unittest import mock

import pytest

from app.auth import Identity


@pytest.fixture(autouse=True)
def existing_api_tests_use_admin_session(request):
    if request.node.get_closest_marker("auth_boundary"):
        yield
        return

    async def authenticated_test_admin(_token):
        return Identity("test-admin", "测试管理员", True)

    with mock.patch("app.main.resolve_identity", side_effect=authenticated_test_admin):
        yield
