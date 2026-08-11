from __future__ import annotations

import http.client
import json
import os
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from .config import FeishuAppConfig


API_BASE = "https://open.feishu.cn/open-apis"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
MACOS_SYSTEM_CA_FILE = "/etc/ssl/cert.pem"
RESOURCE_DOWNLOAD_TIMEOUT = 15


class TokenStore(Protocol):
    def set(self, account: str, value: str) -> None: ...

    def get(self, account: str) -> str | None: ...


class FeishuAPIError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class TokenResult:
    access_token: str
    expires_in: int
    refresh_token: str | None
    refresh_token_expires_in: int | None
    scope: str


class FeishuClient:
    def __init__(
        self,
        config: FeishuAppConfig,
        token_store: TokenStore,
        *,
        timeout: float = 30,
        token_namespace: str | None = None,
    ) -> None:
        self.config = config
        self.token_store = token_store
        self.timeout = timeout
        namespace = (token_namespace or "").strip()
        if ":" in namespace:
            raise ValueError("token_namespace 不能包含冒号")
        self.token_namespace = namespace
        ca_file = os.environ.get("SSL_CERT_FILE", "").strip()
        if not ca_file and os.path.isfile(MACOS_SYSTEM_CA_FILE):
            ca_file = MACOS_SYSTEM_CA_FILE
        self.ssl_context = ssl.create_default_context(cafile=ca_file or None)
        self._tenant_token: str | None = None
        self._tenant_token_expires_at = 0
        self._current_user_open_id: str | None = None

    def account(self, name: str) -> str:
        if self.token_namespace:
            return f"{self.config.app_id}:{self.token_namespace}:{name}"
        return f"{self.config.app_id}:{name}"

    def new_state(self) -> str:
        return secrets.token_urlsafe(32)

    def authorization_url(self, state: str) -> str:
        params = {
            "client_id": self.config.app_id,
            "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str) -> TokenResult:
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.config.app_id,
            "client_secret": self.config.app_secret,
            "code": code,
            "redirect_uri": self.config.redirect_uri,
        }
        result = self._json_request("POST", "/authen/v2/oauth/token", payload=payload, auth=False)
        return self._save_token_result(result)

    def refresh_user_token(self) -> TokenResult:
        refresh_token = self.token_store.get(self.account("refresh_token"))
        if not refresh_token:
            raise FeishuAPIError("没有可用的 refresh_token，请重新执行 auth")
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.config.app_id,
            "client_secret": self.config.app_secret,
            "refresh_token": refresh_token,
        }
        result = self._json_request("POST", "/authen/v2/oauth/token", payload=payload, auth=False)
        return self._save_token_result(result)

    def _save_token_result(self, result: dict[str, Any]) -> TokenResult:
        access_token = str(result.get("access_token") or "")
        if not access_token:
            raise FeishuAPIError("飞书 OAuth 响应中没有 access_token")
        expires_in = int(result.get("expires_in") or 0)
        refresh_token = result.get("refresh_token")
        refresh_expires = result.get("refresh_token_expires_in")
        now = int(time.time())
        self.token_store.set(self.account("access_token"), access_token)
        self.token_store.set(self.account("access_expires_at"), str(now + expires_in))
        if refresh_token:
            self.token_store.set(self.account("refresh_token"), str(refresh_token))
            self.token_store.set(
                self.account("refresh_expires_at"),
                str(now + int(refresh_expires or 0)),
            )
        scope = str(result.get("scope") or "")
        if scope:
            self.token_store.set(self.account("scope"), scope)
        return TokenResult(
            access_token=access_token,
            expires_in=expires_in,
            refresh_token=str(refresh_token) if refresh_token else None,
            refresh_token_expires_in=int(refresh_expires) if refresh_expires else None,
            scope=scope,
        )

    def authorized_scopes(self) -> set[str]:
        return set((self.token_store.get(self.account("scope")) or "").split())

    def user_access_token(self) -> str:
        token = self.token_store.get(self.account("access_token"))
        expires_value = self.token_store.get(self.account("access_expires_at"))
        expires_at = int(expires_value or 0)
        if token and expires_at > int(time.time()) + 60:
            return token
        return self.refresh_user_token().access_token

    def tenant_access_token(self) -> str:
        now = int(time.time())
        if self._tenant_token and self._tenant_token_expires_at > now + 60:
            return self._tenant_token
        result = self._json_request(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            payload={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            auth=False,
        )
        token = str(result.get("tenant_access_token") or "")
        if not token:
            raise FeishuAPIError("飞书响应中没有 tenant_access_token")
        self._tenant_token = token
        self._tenant_token_expires_at = now + int(result.get("expire") or 0)
        return token

    def iter_chat_pages(self) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "page_size": 100,
                "sort_type": "ByCreateTimeAsc",
                "user_id_type": "open_id",
            }
            if page_token:
                params["page_token"] = page_token
            result = self._json_request("GET", "/im/v1/chats", params=params)
            data = result.get("data") or {}
            yield data
            if not data.get("has_more"):
                return
            next_token = data.get("page_token")
            if not next_token or next_token == page_token:
                raise FeishuAPIError("群列表分页返回了无效 page_token")
            page_token = str(next_token)

    def current_user_open_id(self) -> str:
        if self._current_user_open_id:
            return self._current_user_open_id
        result = self._json_request("GET", "/authen/v1/user_info")
        data = result.get("data") or result
        open_id = str(data.get("open_id") or "").strip()
        if not open_id:
            raise FeishuAPIError("飞书用户信息响应中没有 open_id")
        self._current_user_open_id = open_id
        return open_id

    def iter_message_search_pages(
        self,
        *,
        chat_type: str = "p2p",
        page_token: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        if chat_type not in {"p2p", "group"}:
            raise ValueError("chat_type 必须是 p2p 或 group")
        while True:
            params: dict[str, Any] = {
                "page_size": 50,
                "user_id_type": "open_id",
            }
            if page_token:
                params["page_token"] = page_token
            result = self._json_request(
                "POST",
                "/im/v1/messages/search",
                params=params,
                payload={"filter": {"chat_type": chat_type}},
            )
            data = result.get("data") or {}
            yield data
            if not data.get("has_more"):
                return
            next_token = data.get("page_token")
            if not next_token or next_token == page_token:
                raise FeishuAPIError("消息搜索分页返回了无效 page_token")
            page_token = str(next_token)

    def iter_message_pages(
        self,
        container_type: str,
        container_id: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        if container_type not in {"chat", "thread"}:
            raise ValueError("container_type 必须是 chat 或 thread")
        if container_type == "thread" and (start_time is not None or end_time is not None):
            raise ValueError("飞书 thread 历史接口不支持 start_time/end_time")
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "container_id_type": container_type,
                "container_id": container_id,
                "sort_type": "ByCreateTimeAsc",
                "page_size": 50,
                "card_msg_content_type": "user_card_content",
            }
            if start_time is not None:
                params["start_time"] = start_time
            if end_time is not None:
                params["end_time"] = end_time
            if page_token:
                params["page_token"] = page_token
            result = self._json_request("GET", "/im/v1/messages", params=params)
            data = result.get("data") or {}
            yield data
            if not data.get("has_more"):
                return
            next_token = data.get("page_token")
            if not next_token or next_token == page_token:
                raise FeishuAPIError("消息分页返回了无效 page_token")
            page_token = str(next_token)

    def iter_member_pages(self, chat_id: str) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        encoded_chat_id = urllib.parse.quote(chat_id, safe="")
        while True:
            params: dict[str, Any] = {
                "member_id_type": "open_id",
                "page_size": 100,
            }
            if page_token:
                params["page_token"] = page_token
            result = self._json_request(
                "GET", f"/im/v1/chats/{encoded_chat_id}/members", params=params
            )
            data = result.get("data") or {}
            yield data
            if not data.get("has_more"):
                return
            next_token = data.get("page_token")
            if not next_token or next_token == page_token:
                raise FeishuAPIError("群成员分页返回了无效 page_token")
            page_token = str(next_token)

    def iter_wiki_space_pages(self) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 50}
            if page_token:
                params["page_token"] = page_token
            result = self._json_request("GET", "/wiki/v2/spaces", params=params)
            data = result.get("data") or {}
            yield data
            if not data.get("has_more"):
                return
            next_token = data.get("page_token")
            if not next_token or next_token == page_token:
                raise FeishuAPIError("知识空间分页返回了无效 page_token")
            page_token = str(next_token)

    def iter_wiki_node_pages(
        self,
        space_id: str,
        *,
        parent_node_token: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        encoded_space_id = urllib.parse.quote(space_id, safe="")
        while True:
            params: dict[str, Any] = {"page_size": 50}
            if parent_node_token:
                params["parent_node_token"] = parent_node_token
            if page_token:
                params["page_token"] = page_token
            result = self._json_request(
                "GET",
                f"/wiki/v2/spaces/{encoded_space_id}/nodes",
                params=params,
            )
            data = result.get("data") or {}
            yield data
            if not data.get("has_more"):
                return
            next_token = data.get("page_token")
            if not next_token or next_token == page_token:
                raise FeishuAPIError("知识库节点分页返回了无效 page_token")
            page_token = str(next_token)

    def get_docx_document(self, document_id: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(document_id, safe="")
        result = self._json_request("GET", f"/docx/v1/documents/{encoded}")
        data = result.get("data") or {}
        return dict(data.get("document") or data)

    def get_docx_raw_content(self, document_id: str) -> str:
        encoded = urllib.parse.quote(document_id, safe="")
        result = self._json_request("GET", f"/docx/v1/documents/{encoded}/raw_content")
        data = result.get("data") or {}
        return str(data.get("content") or "")

    def iter_docx_block_pages(self, document_id: str) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        encoded = urllib.parse.quote(document_id, safe="")
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            result = self._json_request(
                "GET", f"/docx/v1/documents/{encoded}/blocks", params=params
            )
            data = result.get("data") or {}
            yield data
            if not data.get("has_more"):
                return
            next_token = data.get("page_token")
            if not next_token or next_token == page_token:
                raise FeishuAPIError("新版文档块分页返回了无效 page_token")
            page_token = str(next_token)

    def open_drive_file(self, file_token: str):
        encoded = urllib.parse.quote(file_token, safe="")
        return self._open_binary(f"/drive/v1/files/{encoded}/download")

    def open_drive_media(self, file_token: str):
        encoded = urllib.parse.quote(file_token, safe="")
        return self._open_binary(f"/drive/v1/medias/{encoded}/download")

    def open_resource(self, message_id: str, file_key: str, resource_type: str):
        if resource_type not in {"image", "file"}:
            raise ValueError("resource_type 必须是 image 或 file")
        path = (
            f"/im/v1/messages/{urllib.parse.quote(message_id, safe='')}/resources/"
            f"{urllib.parse.quote(file_key, safe='')}"
        )
        return self._open_binary(path, params={"type": resource_type})

    def _open_binary(self, path: str, *, params: dict[str, Any] | None = None):
        url = f"{API_BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            method="GET",
            # Keep resource visibility aligned with the OAuth user's access.
            headers={"Authorization": f"Bearer {self.user_access_token()}"},
        )
        try:
            return urllib.request.urlopen(
                request,
                timeout=max(self.timeout, RESOURCE_DOWNLOAD_TIMEOUT),
                context=self.ssl_context,
            )
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise FeishuAPIError(f"附件请求失败：{exc.reason}") from exc

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if auth:
            headers["Authorization"] = f"Bearer {self.user_access_token()}"

        for attempt in range(4):
            request = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout,
                    context=self.ssl_context,
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if int(result.get("code") or 0) != 0:
                    error = FeishuAPIError(
                        f"飞书 API {path} 返回错误：{result.get('msg') or 'unknown'}",
                        code=int(result.get("code") or 0),
                    )
                    if self._is_rate_limit_error(error) and attempt < 3:
                        time.sleep(min(2**attempt, 10))
                        continue
                    raise error
                return result
            except urllib.error.HTTPError as exc:
                error = self._http_error(exc)
                if (
                    exc.code == 429
                    or 500 <= exc.code < 600
                    or self._is_rate_limit_error(error)
                ) and attempt < 3:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = float(retry_after) if retry_after else float(2**attempt)
                    except ValueError:
                        delay = float(2**attempt)
                    time.sleep(min(delay, 10))
                    continue
                raise error from exc
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(min(2**attempt, 5))
                    continue
                raise FeishuAPIError(f"飞书 API 网络请求失败：{exc.reason}") from exc
            except (ConnectionError, http.client.HTTPException) as exc:
                if attempt < 3:
                    time.sleep(min(2**attempt, 5))
                    continue
                raise FeishuAPIError(f"飞书 API 网络请求失败：{exc}") from exc
            except TimeoutError as exc:
                if attempt < 3:
                    time.sleep(min(2**attempt, 5))
                    continue
                raise FeishuAPIError("飞书 API 网络请求失败：请求超时") from exc
        raise FeishuAPIError("飞书 API 请求重试次数已用尽")

    @staticmethod
    def _is_rate_limit_error(exc: FeishuAPIError) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in ("too many request", "rate limit", "frequency limit", "频率限制")
        )

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> FeishuAPIError:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
            code = int(payload.get("code") or 0)
            message = payload.get("msg") or payload.get("message") or str(exc.reason)
        except (ValueError, UnicodeDecodeError):
            code = None
            message = str(exc.reason)
        return FeishuAPIError(
            f"飞书 API HTTP {exc.code}：{message}",
            status=exc.code,
            code=code,
        )
