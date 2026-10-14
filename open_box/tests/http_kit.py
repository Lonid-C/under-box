"""测试用的公开网络预检隔离：虚构域名解析到公开地址，不发起真实 DNS 查询。

任务书要求安全出网测试"注入假 DNS 解析器"；这里集中提供，避免每个用例
重复定义。仅用于测试进程，不影响生产默认解析器。
"""
import socket
from unittest.mock import patch

PUBLIC_DNS = lambda host, port, type=None: [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def fake_public_dns(test):
    """把 app.safe_http.DNS_RESOLVER 替换为假解析器，测试结束自动还原。"""
    patcher = patch("app.safe_http.DNS_RESOLVER", PUBLIC_DNS)
    patcher.start()
    test.addCleanup(patcher.stop)
