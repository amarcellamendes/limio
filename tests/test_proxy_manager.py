import asyncio
import time
import pytest
from backend.proxy_manager import ProxyManager, ProxyEntry


def make_manager_with_proxies(entries):
    """Cria ProxyManager sem API key, injeta proxies diretamente."""
    m = ProxyManager(api_key="", static_url="")
    m._proxies = entries
    m._last_refresh = time.time()
    return m


def test_get_proxy_returns_none_when_empty():
    m = ProxyManager(api_key="", static_url="")
    m._last_refresh = time.time()
    result = asyncio.run(m.get_proxy())
    assert result is None


def test_get_proxy_prefers_brazil():
    entries = [
        ProxyEntry(url="http://u:p@br1:8080", country_code="BR"),
        ProxyEntry(url="http://u:p@us1:8080", country_code="US"),
    ]
    m = make_manager_with_proxies(entries)
    result = asyncio.run(m.get_proxy(prefer_brazil=True))
    assert result == "http://u:p@br1:8080"


def test_get_proxy_falls_back_when_no_brazil():
    entries = [ProxyEntry(url="http://u:p@us1:8080", country_code="US")]
    m = make_manager_with_proxies(entries)
    result = asyncio.run(m.get_proxy(prefer_brazil=True))
    assert result == "http://u:p@us1:8080"


def test_record_failure_cools_proxy_after_max_consecutive():
    entries = [ProxyEntry(url="http://u:p@bad:8080", country_code="US")]
    m = make_manager_with_proxies(entries)
    for _ in range(ProxyManager.MAX_CONSECUTIVE_FAILURES):
        m.record_failure("http://u:p@bad:8080")
    entry = m._proxies[0]
    assert entry.cooling_until > time.time()
    assert not entry.is_available


def test_record_success_resets_consecutive_failures():
    entries = [ProxyEntry(url="http://u:p@ok:8080", country_code="BR")]
    m = make_manager_with_proxies(entries)
    m.record_failure("http://u:p@ok:8080")
    m.record_failure("http://u:p@ok:8080")
    m.record_success("http://u:p@ok:8080", elapsed_ms=200.0)
    assert m._consecutive_failures.get("http://u:p@ok:8080", 0) == 0


def test_stats_hides_credentials():
    entries = [ProxyEntry(url="http://user:secret@host:8080", country_code="BR")]
    m = make_manager_with_proxies(entries)
    stats = m.stats()
    assert "secret" not in stats[0]["url"]
    assert "host:8080" in stats[0]["url"]


def test_cooled_proxy_skipped_when_others_available():
    entries = [
        ProxyEntry(url="http://u:p@bad:8080", country_code="US",
                   cooling_until=time.time() + 9999),
        ProxyEntry(url="http://u:p@good:8080", country_code="US"),
    ]
    m = make_manager_with_proxies(entries)
    result = asyncio.run(m.get_proxy(prefer_brazil=False))
    assert result == "http://u:p@good:8080"
