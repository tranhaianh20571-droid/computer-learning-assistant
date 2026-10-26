"""代理 Fake-IP 统一策略：仅域名解析结果放行，直填地址与私网应答继续拒绝。"""
import socket

import pytest

from backend.net_policy import addresses_allowed, is_literal_address
from backend.materials.errors import MaterialsError
from backend.materials.source_clients import classify_url, public_resolver
from backend.slice1.adapters.base import AdapterError, validate_endpoint


def dns(monkeypatch, *addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "",
         (address, 443)) for address in addresses])


@pytest.mark.parametrize("address", ["198.18.0.42", "198.19.255.254", "8.8.8.8", "2606:4700:4700::1111"])
def test_dns_name_accepts_public_or_proxy_answer(address):
    assert addresses_allowed([address], from_hostname=True)


@pytest.mark.parametrize("address", ["198.18.0.42", "198.19.255.254", "127.0.0.1", "10.0.0.1",
    "169.254.169.254", "::1", "::ffff:127.0.0.1", "fc00::1", "fe80::1"])
def test_proxy_answer_is_not_a_literal_target(address):
    assert not addresses_allowed([address], from_hostname=False)
    assert is_literal_address(address)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "fe80::1",
    "224.0.0.1", "ff0e::1", "240.0.0.1", "::"])
def test_private_answers_stay_blocked_even_for_names(address):
    assert not addresses_allowed([address], from_hostname=True)


def test_mixed_answers_are_rejected_as_a_whole():
    assert not addresses_allowed(["198.18.0.42", "127.0.0.1"], from_hostname=True)
    assert not addresses_allowed([], from_hostname=True)


def test_public_resolver_accepts_proxy_answer_for_a_page_name(monkeypatch):
    dns(monkeypatch, "198.18.0.42")
    assert public_resolver("zh.synthetic.test", 443, 0, socket.SOCK_STREAM)


@pytest.mark.parametrize("answers", [["127.0.0.1"], ["169.254.169.254"],
    ["198.18.0.42", "10.0.0.1"]])
def test_public_resolver_rejects_private_or_mixed_answers(monkeypatch, answers):
    dns(monkeypatch, *answers)
    with pytest.raises(ValueError):
        public_resolver("zh.synthetic.test", 443, 0, socket.SOCK_STREAM)


def test_public_resolver_rejects_a_literal_proxy_address(monkeypatch):
    dns(monkeypatch, "198.18.0.42")
    with pytest.raises(ValueError):
        public_resolver("198.18.0.42", 443, 0, socket.SOCK_STREAM)


def test_classify_url_still_rejects_literal_proxy_address():
    with pytest.raises(MaterialsError) as failure:
        classify_url("https://198.18.0.42/page")
    assert failure.value.code == "invalid_field"


def test_adapter_endpoint_accepts_proxy_answer_for_a_name(monkeypatch):
    dns(monkeypatch, "198.18.0.42")
    validate_endpoint("https://media.synthetic.test/hook")


@pytest.mark.parametrize("url", ["https://198.18.0.42/hook", "http://media.synthetic.test/hook"])
def test_adapter_endpoint_rejects_literal_proxy_or_plain_http(monkeypatch, url):
    dns(monkeypatch, "198.18.0.42")
    with pytest.raises(AdapterError):
        validate_endpoint(url)


def test_adapter_endpoint_rejects_private_answers(monkeypatch):
    dns(monkeypatch, "127.0.0.1")
    with pytest.raises(AdapterError):
        validate_endpoint("https://media.synthetic.test/hook")
