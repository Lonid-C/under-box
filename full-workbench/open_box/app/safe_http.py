"""Preflight URL policy for public fetching; transports/DNS are injectable in tests."""
from __future__ import annotations
import ipaddress
import socket
import httpx
from urllib.parse import urlsplit, urljoin


# 测试可用假解析器替换（任务书要求"注入假 DNS 解析器"），生产保持系统解析。
DNS_RESOLVER = socket.getaddrinfo


class UnsafeURL(ValueError):
    pass


def allowed_ip(value):
    ip = ipaddress.ip_address(value)
    return ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified or str(ip) == "169.254.169.254")


def validate_url(url, *, resolver=None):
    resolver = resolver or DNS_RESOLVER
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError as exc:
        raise UnsafeURL("无效 URL 或端口") from exc
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise UnsafeURL("只允许不带用户信息的 http/https URL")
    if port not in {None, 80, 443}:
        raise UnsafeURL("不接受该端口")
    try:
        addresses = {item[4][0] for item in resolver(p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise UnsafeURL("域名无法安全解析") from exc
    if not addresses or not all(allowed_ip(address) for address in addresses):
        raise UnsafeURL("目标地址属于本机、内网、保留或元数据网络")
    return p


def validate_redirect(previous, target, *, resolver=None):
    validate_url(target, resolver=resolver)
    # A redirect may change host, but it must pass the same destination check.
    return target


class PublicTransport(httpx.BaseTransport):
    """Resolve once, validate all answers, and connect to that exact numeric IP.

    Preserve Host and TLS SNI; trust_env=False prevents ambient proxies from
    resolving the hostname again. Fake DNS/transport is explicitly injectable.
    """
    def __init__(self, *, resolver=socket.getaddrinfo, inner=None, verify=True):
        self.resolver = resolver
        self.inner = inner or httpx.HTTPTransport(verify=verify, retries=0)

    def handle_request(self, request):
        parsed = urlsplit(str(request.url))
        answers = []
        def resolve_once(host, port, **kwargs):
            answers.extend(self.resolver(host, port, **kwargs))
            return answers
        validate_url(str(request.url), resolver=resolve_once)
        addresses = [item[4][0] for item in answers]
        if not addresses or not all(allowed_ip(ip) for ip in addresses):
            raise UnsafeURL("连接目标不在公开网络")
        # Use the one checked resolution directly; never resolve at connection time.
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        extensions = {**request.extensions, "sni_hostname": parsed.hostname}
        pinned = httpx.Request(request.method, request.url.copy_with(host=addresses[0]),
                               headers=headers, stream=request.stream, extensions=extensions)
        response = self.inner.handle_request(pinned)
        return response

    def close(self):
        self.inner.close()


def public_request(method, url, *, max_bytes=32 * 1024 * 1024, max_redirects=3,
                   redirect_check=None, transport=None, timeout=None, headers=None,
                   data=None, resolver=None, **kwargs):
    """逐跳校验的公开请求。

    每次请求前对**实际目标**做 DNS/IP 预检（`DNS_RESOLVER` 可注入假解析器）；
    默认经由模块级 `httpx.get` / `httpx.post` 发出（既有测试的 mock seam 保留）。
    固定地址 API 可在调用处直接使用 `PublicTransport` 钉住解析结果。
    """
    resolver = resolver or DNS_RESOLVER
    client = (httpx.Client(transport=transport, trust_env=False, follow_redirects=False)
              if transport is not None else None)
    kwargs.pop("verify", None)  # 自定义 transport 在构造时决定校验方式
    try:
        for hop in range(max_redirects + 1):
            validate_url(url, resolver=resolver)
            if client is not None:
                response = client.request(method, url, headers=headers, timeout=timeout,
                                          data=data, **kwargs)
            elif method.upper() == "GET":
                response = httpx.get(url, headers=headers, timeout=timeout,
                                     follow_redirects=False, **kwargs)
            else:
                response = httpx.post(url, headers=headers, timeout=timeout,
                                      follow_redirects=False, data=data, **kwargs)
            status = getattr(response, "status_code", 200)
            if status in {301, 302, 303, 307, 308}:
                response_headers = getattr(response, "headers", {}) or {}
                location = response_headers.get("location", "") if hasattr(response_headers, "get") else ""
                if hop >= max_redirects or method.upper() != "GET" or not location:
                    raise UnsafeURL("重定向次数超限或目标不合法")
                target = urljoin(url, location)
                if redirect_check:
                    redirect_check(target)
                url = target
                continue
            content = getattr(response, "content", b"")
            if isinstance(content, (bytes, bytearray)) and len(content) > max_bytes:
                raise UnsafeURL("下载内容超过大小上限")
            return response
        raise UnsafeURL("重定向次数超限")
    finally:
        if client is not None:
            client.close()
