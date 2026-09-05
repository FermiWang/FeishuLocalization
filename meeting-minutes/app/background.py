"""Read public meeting pages as untrusted background data, without a browser."""

import hashlib
import http.client
import io
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


MAX_URL_CHARS = 8192
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 12000
FETCH_TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 3
_DNS_SLOTS = threading.BoundedSemaphore(4)


class BackgroundFetchError(ValueError):
    """A safe, user-readable failure to import a public meeting page."""


def extract_background_urls(background: str) -> list[str]:
    """Extract ordered, unique HTTP(S) links from prose or Markdown."""
    found = []
    # Markdown escapes in pasted email links do not form part of the URL.
    source = str(background or "").replace(r"\&", "&")
    for match in re.finditer(r"https?://[^\s<>\"'\u3000]+", source, re.I):
        url = match.group(0).rstrip(".,;!，。；！、）】》")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        if url not in found:
            found.append(url)
    return found


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise BackgroundFetchError("读取会议网页超时，请稍后重试或粘贴网页介绍。")
    return left


def _validated_url(url):
    if not isinstance(url, str) or len(url) > MAX_URL_CHARS:
        raise BackgroundFetchError("会议网页链接不能超过 8192 个字符。")
    url = url.strip()
    if not url or re.search(r"[\x00-\x20\x7f\\]", url):
        raise BackgroundFetchError("会议网页链接包含空格或无效字符。")
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"}:
            raise BackgroundFetchError("会议背景仅支持公开的 http 或 https 网页。")
        if parts.username is not None or parts.password is not None:
            raise BackgroundFetchError("会议网页链接不能包含用户名或密码。")
        host = (parts.hostname or "").rstrip(".")
        if not host or "%" in host:
            raise BackgroundFetchError("会议网页主机名无效。")
        host = host.encode("idna").decode("ascii").lower()
        scheme = parts.scheme.lower()
        port = parts.port if parts.port is not None else (443 if scheme == "https" else 80)
        if port != (443 if scheme == "https" else 80):
            raise BackgroundFetchError("会议网页仅支持标准网页端口 80（HTTP）或 443（HTTPS）。")
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, BackgroundFetchError):
            raise
        raise BackgroundFetchError("会议网页链接格式无效。") from exc
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise BackgroundFetchError("会议背景仅能读取公开网页，不能访问本机或内网地址。")
    netloc = f"[{host}]" if ":" in host else host
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
    normalized = urlunsplit((scheme, netloc, path, query, ""))
    return normalized, scheme, host, port, path + ("?" + query if query else "")


def _public_address(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # Transition addresses can hide an IPv4 destination from IP checks.
        if ip.ipv4_mapped or ip.sixtofour or ip.teredo:
            return False
    return True


def _resolve_public(host, port, deadline):
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise BackgroundFetchError("会议网页域名查询繁忙，请稍后重试。")
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put((True, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except Exception as exc:
            result.put((False, exc))
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, name="meeting-background-dns", daemon=True).start()
    try:
        success, records = result.get(timeout=_remaining(deadline))
    except queue.Empty as exc:
        raise BackgroundFetchError("会议网页域名查询超时，请稍后重试。") from exc
    if not success:
        raise BackgroundFetchError("无法解析会议网页域名，请检查链接。") from records
    if not records or any(
        family not in (socket.AF_INET, socket.AF_INET6)
        or not _public_address(address[0])
        for family, _, _, _, address in records
    ):
        raise BackgroundFetchError("会议背景仅能读取公开网页，不能访问本机或内网地址。")
    # Every answer was checked. Connect to these exact numeric addresses;
    # HTTP/TLS must not perform a second DNS lookup (DNS rebinding).
    return list(dict.fromkeys((family, address) for family, _, _, _, address in records))


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline
        # SocketIO owns a reference so HTTPConnection.close() after response
        # headers does not invalidate a Connection: close response body.
        self.raw = sock.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        return self.raw.readinto(buffer)

    def close(self):
        if not self.closed:
            self.raw.close()
        super().close()


class _DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def makefile(self, mode):
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))

    def sendall(self, data):
        self.sock.settimeout(_remaining(self.deadline))
        return self.sock.sendall(data)

    def close(self):
        self.sock.close()


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, scheme, host, port, addresses, deadline):
        super().__init__(host, port, timeout=_remaining(deadline))
        self.scheme = scheme
        self.addresses = addresses
        self.deadline = deadline

    def connect(self):
        last_error = None
        for family, address in self.addresses:
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(_remaining(self.deadline))
                sock.connect(address)
                if self.scheme == "https":
                    sock.settimeout(_remaining(self.deadline))
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
                self.sock = _DeadlineSocket(sock, self.deadline)
                return
            except (OSError, BackgroundFetchError) as exc:
                sock.close()
                last_error = exc
        raise last_error or BackgroundFetchError("无法连接会议网页。")


_SKIP_TAGS = {
    "script", "style", "nav", "footer", "form", "iframe", "svg",
    "canvas", "noscript", "template", "button", "select", "textarea",
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "br", "hr"}


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.main = []
        self.title = []
        self.heading = []

    def _append(self, text):
        self.text.append(text)
        if any(tag == "main" for tag, _ in self.stack):
            self.main.append(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        blocked = any(skip for _, skip in self.stack) or tag in _SKIP_TAGS
        blocked = blocked or "hidden" in attrs or attrs.get("aria-hidden", "").lower() == "true"
        if tag not in _VOID_TAGS:
            self.stack.append((tag, blocked))
        if not blocked and tag in _BLOCK_TAGS:
            self._append("\n")
        elif not blocked and tag in {"td", "th"}:
            self._append(" | ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        blocked = any(skip for _, skip in self.stack)
        if not blocked and tag in _BLOCK_TAGS:
            self._append("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if any(skip for _, skip in self.stack):
            return
        if any(tag == "title" for tag, _ in self.stack):
            self.title.append(data)
        elif not any(tag == "head" for tag, _ in self.stack):
            self._append(data)
            if any(tag == "h1" for tag, _ in self.stack):
                self.heading.append(data)


def _clean_text(text):
    return "\n".join(line for line in (re.sub(r"\s+", " ", line).strip() for line in text.splitlines()) if line)


def _decode_body(body, content_type):
    charset_match = re.search(r"charset\s*=\s*[\"']?([^\s;\"']+)", content_type, re.I)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def fetch_background(url: str) -> dict:
    """Fetch one public page. Returned text remains untrusted source material."""
    deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS
    original, *_ = _validated_url(url)
    current = original
    try:
        for hop in range(MAX_REDIRECTS + 1):
            current, scheme, host, port, target = _validated_url(current)
            addresses = _resolve_public(host, port, deadline)
            connection = _PinnedConnection(scheme, host, port, addresses, deadline)
            try:
                connection.request("GET", target, headers={
                    "User-Agent": "MeetingRecords/1.0 (+public meeting background reader)",
                    "Accept": "text/html, application/xhtml+xml, text/plain;q=0.8",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or hop == MAX_REDIRECTS:
                        raise BackgroundFetchError("会议网页重定向过多或缺少跳转地址，请使用最终公开网页链接。")
                    if re.search(r"[\x00-\x20\x7f\\]", location):
                        raise BackgroundFetchError("会议网页跳转地址包含无效字符。")
                    current = urljoin(current, location)
                    continue
                if response.status != 200:
                    detail = "网页拒绝自动读取，可粘贴网页介绍" if response.status in {401, 403, 429} else "请检查链接或稍后重试"
                    raise BackgroundFetchError(f"会议网页返回 HTTP {response.status}，{detail}。")
                content_type = response.getheader("Content-Type", "").lower()
                media_type = content_type.split(";", 1)[0].strip()
                if media_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                    raise BackgroundFetchError("会议背景链接必须指向文字网页，不能是录音或下载文件。")
                if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise BackgroundFetchError("会议网页未提供可直接读取的文字响应，请粘贴网页介绍。")
                length = response.getheader("Content-Length")
                if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                    raise BackgroundFetchError("会议网页超过 2 MB 读取上限，请粘贴相关介绍。")
                body = bytearray()
                while True:
                    _remaining(deadline)
                    chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(body)))
                    if not chunk:
                        if getattr(response, "length", None):
                            raise BackgroundFetchError("会议网页传输未完成，请稍后重试。")
                        break
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise BackgroundFetchError("会议网页超过 2 MB 读取上限，请粘贴相关介绍。")
                decoded = _decode_body(bytes(body), content_type)
                if media_type == "text/plain":
                    title, text = "", _clean_text(decoded)
                else:
                    page = _PageText()
                    page.feed(decoded)
                    title = _clean_text("".join(page.heading or page.title))[:500]
                    text = _clean_text("".join(page.main or page.text))
                if not text:
                    raise BackgroundFetchError("会议网页没有可读取正文；可能需要登录或脚本加载，请粘贴网页介绍。")
                truncated = len(text) > MAX_TEXT_CHARS
                text = text[:MAX_TEXT_CHARS]
                _remaining(deadline)
                return {
                    "url": original, "final_url": current, "title": title, "text": text,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "content_hash": hashlib.sha256((title + "\n" + text).encode("utf-8")).hexdigest(),
                    "truncated": truncated,
                    "notices": ["网页正文较长，仅保存前 12,000 个字符作为会议背景。"] if truncated else [],
                }
            finally:
                connection.close()
    except BackgroundFetchError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise BackgroundFetchError("读取会议网页超时，请稍后重试或粘贴网页介绍。") from exc
    except ssl.SSLError as exc:
        raise BackgroundFetchError("会议网页安全证书验证失败，请检查公开网页链接。") from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise BackgroundFetchError("无法读取会议网页，请检查链接或粘贴网页介绍。") from exc
    raise BackgroundFetchError("无法读取会议网页。")
