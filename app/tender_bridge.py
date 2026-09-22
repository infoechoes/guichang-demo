"""Loopback bridge from the multi-user gateway to the tender workbench.

The bridge deliberately owns no session state.  ``revalidate`` is supplied by
the caller and is run before the request, periodically during large transfers,
and before returning the response.
"""
from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import re
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit


class TenderBridgeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class TenderBridge:
    HOST = "127.0.0.1"
    PORT = 8769
    MAX_UPLOAD = 1024 ** 3
    MAX_JSON = 4 * 1024 ** 2
    _PATHS = {
        "/", "/app.js", "/style.css", "/api/access", "/api/bootstrap",
        "/api/project", "/api/job", "/api/calls", "/api/library",
        "/api/source", "/api/project-source", "/api/image", "/api/original",
        "/api/download", "/api/health", "/api/upload", "/api/model-profiles",
    }
    _POST_ACTIONS = {
        "create", "parameters", "project-meta", "import-existing", "import-upload",
        "analyze", "analyze-model", "match", "requirement", "add-requirement",
        "generate", "edit-section", "restore", "references", "fact-review",
        "evidence-review", "attach", "attachments", "audit", "export",
        "configure", "connection-test", "question", "model-add", "model-activate",
    }
    _ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")

    def __init__(self, config_path, *, port=None, test_only_port=False, temp_dir=None):
        self.config_path = Path(config_path)
        if self.config_path.is_dir():
            self.config_path = self.config_path / "bridge-config.json"
        self.host = self.HOST
        self.port = self.PORT if port is None else port
        if port is not None and (not test_only_port or type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("仅测试模式可覆盖标书服务端口")
        self.temp_dir = Path(temp_dir) if temp_dir else None
        self._secret = None

    def _load_secret(self):
        if self._secret is not None:
            return self._secret
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            secret = config["secret"]
        except Exception as exc:
            raise TenderBridgeError("标书桥接配置无效", 503) from exc
        if not isinstance(secret, str) or len(secret) < 32:
            raise TenderBridgeError("标书桥接配置无效", 503)
        self._secret = secret
        return secret

    @staticmethod
    def _check_user(user):
        if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
            raise TenderBridgeError("登录状态无效", 401)
        if user.get("role") != "admin" and user.get("department") != "bid":
            raise TenderBridgeError("仅投标部和管理员可使用标书工作台", 403)

    @staticmethod
    def _check_csrf(csrf):
        if not isinstance(csrf, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", csrf):
            raise TenderBridgeError("登录状态无效", 401)

    def _auth_headers(self, user, csrf, mutation):
        self._check_user(user); self._check_csrf(csrf)
        identity = {"id": user["id"], "role": user.get("role"),
                    "department": user.get("department"),
                    "displayName": user.get("display_name")}
        encoded = base64.urlsafe_b64encode(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).decode().rstrip("=")
        headers = {"X-Bid-Bridge-Key": self._load_secret(), "X-Bid-User": encoded, "X-Bid-CSRF": csrf}
        if mutation:
            headers["X-Workbench-Token"] = csrf
        return headers

    @classmethod
    def _target(cls, target, method):
        if not isinstance(target, str) or not target or "\\" in target:
            raise TenderBridgeError("标书路径无效", 400)
        if target.startswith("//") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
            raise TenderBridgeError("标书路径无效", 400)
        # Encoded separators/dots are rejected rather than decoded repeatedly.
        parsed = urlsplit(target if target.startswith("/") else "/" + target)
        if parsed.scheme or parsed.netloc or "\\" in parsed.path:
            raise TenderBridgeError("标书路径无效", 400)
        if re.search(r"%(?:2f|2F|5c|5C|2e|2E)", parsed.path):
            raise TenderBridgeError("标书路径无效", 400)
        path = unquote(parsed.path)
        parts = path.split("/")[1:]
        if "%" in path or (path != "/" and any(part in ("", ".", "..") for part in parts)):
            raise TenderBridgeError("标书路径无效", 400)
        if path not in cls._PATHS:
            if method != "POST" or path != "/api/action":
                raise TenderBridgeError("标书接口未开放", 403)
        if method not in ("GET", "POST"):
            raise TenderBridgeError("标书接口未开放", 405)
        if method == "POST" and path not in ("/api/upload", "/api/action"):
            raise TenderBridgeError("标书接口未开放", 405)
        if method == "GET" and path == "/api/upload":
            raise TenderBridgeError("标书接口未开放", 405)
        return parsed.geturl()

    @staticmethod
    def _revalidate(callback, user, csrf):
        if callback is None:
            return user
        result = callback()
        if result is False or result is None:
            raise TenderBridgeError("登录状态已变化，请重新进入标书工作台", 403)
        return result if isinstance(result, dict) else user

    def request(self, user, csrf, method, target, input_stream=None, size=0, content_type=None, revalidate=None):
        method = str(method).upper()
        route = self._target(target, method)
        mutation = method == "POST"
        if not isinstance(size, int) or size < 0:
            raise TenderBridgeError("请求大小无效", 400)
        if mutation:
            limit = self.MAX_UPLOAD if urlsplit(route).path == "/api/upload" else self.MAX_JSON
            if not 0 < size <= limit:
                raise TenderBridgeError("请求过大或为空", 400)
            if not isinstance(content_type, str) or (urlsplit(route).path != "/api/upload" and content_type.split(";", 1)[0].lower() != "application/json"):
                raise TenderBridgeError("请求格式无效", 400)
        user = self._revalidate(revalidate, user, csrf)
        headers = self._auth_headers(user, csrf, mutation)
        request_path = route
        input_file = None
        response_file = None
        try:
            if mutation:
                if input_stream is None or not hasattr(input_stream, "read"):
                    raise TenderBridgeError("请求输入无效", 400)
                input_file = tempfile.NamedTemporaryFile(prefix="tender-in-", dir=str(self.temp_dir) if self.temp_dir else None, delete=False)
                remaining = size; total = 0; last_check = time.monotonic(); last_bytes = 0
                while remaining:
                    chunk = input_stream.read(min(1024 * 1024, remaining))
                    if not chunk: raise TenderBridgeError("请求不完整", 400)
                    input_file.write(chunk); remaining -= len(chunk); total += len(chunk)
                    if total - last_bytes >= 4 * 1024 * 1024 or time.monotonic() - last_check >= 2:
                        user = self._revalidate(revalidate, user, csrf); headers = self._auth_headers(user, csrf, mutation)
                        last_check, last_bytes = time.monotonic(), total
                input_file.flush(); input_file.seek(0)
                if urlsplit(route).path == "/api/action":
                    try:
                        payload = json.load(input_file)
                    except Exception as exc:
                        raise TenderBridgeError("请求需为JSON对象", 400) from exc
                    if not isinstance(payload, dict) or payload.get("action") not in self._POST_ACTIONS:
                        raise TenderBridgeError("标书操作未开放", 403)
                    input_file.seek(0)
                user = self._revalidate(revalidate, user, csrf)
                headers = self._auth_headers(user, csrf, mutation)
                headers["Content-Type"] = content_type
                headers["Content-Length"] = str(size)
            conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
            try:
                if mutation:
                    conn.putrequest(method, request_path)
                    for key, value in headers.items(): conn.putheader(key, value)
                    conn.endheaders()
                    sent = 0; last_check = time.monotonic()
                    while True:
                        chunk = input_file.read(1024 * 1024)
                        if not chunk: break
                        conn.send(chunk); sent += len(chunk)
                        if sent - last_bytes >= 4 * 1024 * 1024 or time.monotonic() - last_check >= 2:
                            user = self._revalidate(revalidate, user, csrf)
                            last_check, last_bytes = time.monotonic(), sent
                else:
                    conn.request(method, request_path, headers=headers)
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    raise TenderBridgeError("标书服务不允许跳转", 502)
                if response.getheader("Content-Length") and int(response.getheader("Content-Length")) > self.MAX_UPLOAD:
                    raise TenderBridgeError("标书响应过大", 502)
                # Keep the handle open for the gateway; closing it removes the
                # response file even on Windows.
                response_file = tempfile.NamedTemporaryFile(prefix="tender-out-", dir=str(self.temp_dir) if self.temp_dir else None, delete=True)
                received = 0
                read_bytes = 0; last_check = time.monotonic()
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk: break
                    received += len(chunk)
                    if received > self.MAX_UPLOAD: raise TenderBridgeError("标书响应过大", 502)
                    response_file.write(chunk)
                    read_bytes += len(chunk)
                    if read_bytes >= 4 * 1024 * 1024 or time.monotonic() - last_check >= 2:
                        user = self._revalidate(revalidate, user, csrf)
                        read_bytes, last_check = 0, time.monotonic()
                response_file.flush(); response_file.seek(0)
                user = self._revalidate(revalidate, user, csrf)
                mime = response.getheader("Content-Type") or "application/octet-stream"
                extra = {}
                disposition = response.getheader("Content-Disposition")
                if disposition: extra["Content-Disposition"] = disposition
                return response.status, response_file, mime, extra
            finally:
                conn.close()
        except Exception:
            if response_file:
                try:
                    response_file.close()
                    Path(response_file.name if hasattr(response_file, "name") else response_file).unlink(missing_ok=True)
                except Exception: pass
            raise
        finally:
            if input_file:
                name = input_file.name
                input_file.close()
                Path(name).unlink(missing_ok=True)
