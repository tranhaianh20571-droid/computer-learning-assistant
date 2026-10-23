"""出站目标地址策略：公网地址可用，代理 Fake-IP 仅在域名解析结果中放行。

本机代理（Clash/Mihomo 等）常用 198.18.0.0/15 作为 Fake-IP 应答所有域名。
该地址段不是全局可路由地址，但它是域名经代理解析后的正常结果，因此：

- 域名解析得到 198.18.0.0/15 时允许（TLS 证书仍按原域名校验，SNI 固定）；
- URL 直接写 198.18.0.0/15 或其他非公网地址时拒绝；
- 回环、私网、链路本地、保留、组播、元数据地址无论来源都拒绝；
- 混合解析（同时含私网）整体拒绝。

凭证、重定向、环境代理与响应大小的既有约束不因本策略放宽。
"""

from __future__ import annotations

import ipaddress

PROXY_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def parse_address(value) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把地址字面量或已解析地址统一为 IPv4 地址（IPv4 映射的 IPv6 会解包）。"""
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        address = value
    else:
        try:
            address = ipaddress.ip_address(str(value))
        except (TypeError, ValueError):
            return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_literal_address(host) -> bool:
    """True 表示主机本身就是 IP 字面量，而不是需要解析的域名。"""
    return parse_address(host) is not None


def allowed_address(value, *, from_hostname: bool) -> bool:
    address = parse_address(value)
    if address is None:
        return False
    if address.is_global:
        return True
    return from_hostname and address in PROXY_FAKE_IP


def addresses_allowed(values, *, from_hostname: bool) -> bool:
    """所有解析结果都必须是公网地址或域名作用域下的代理 Fake-IP。"""
    addresses = [parse_address(value) for value in values]
    return bool(addresses) and all(
        address is not None and allowed_address(address, from_hostname=from_hostname)
        for address in addresses)
