"""
ProxyManager: pool de proxies Webshare com rotação automática,
preferência por IPs brasileiros e monitoramento de latência.
Credenciais via env vars WEBSHARE_API_KEY e PROXY_RESIDENCIAL_URL.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx


@dataclass
class ProxyEntry:
    url: str                     # http://user:pass@host:port
    country_code: str            # "BR", "US", "" …
    failures: int = 0
    successes: int = 0
    total_response_ms: float = 0.0
    last_used: float = 0.0
    cooling_until: float = 0.0   # epoch — quando o cooling-off termina

    @property
    def is_available(self) -> bool:
        return time.time() >= self.cooling_until

    @property
    def avg_response_ms(self) -> float:
        return self.total_response_ms / max(self.successes, 1)

    @property
    def failure_rate(self) -> float:
        total = self.successes + self.failures
        return self.failures / total if total else 0.0


class ProxyManager:
    """Gerencia pool de proxies com rotação, métricas e preferência BR."""

    MAX_CONSECUTIVE_FAILURES = 3
    COOLING_PERIOD_S = 300        # 5 min de cooling após falhas consecutivas
    REFRESH_INTERVAL_S = 3600     # Recarrega lista Webshare a cada 1h

    def __init__(self, api_key: str, static_url: str = "") -> None:
        self._api_key = api_key
        self._static_url = static_url
        self._proxies: list[ProxyEntry] = []
        self._lock = asyncio.Lock()
        self._last_refresh: float = 0.0
        self._consecutive_failures: dict[str, int] = {}

    # ── Carregamento ────────────────────────────────────────────────────────

    async def _fetch_webshare(self) -> list[ProxyEntry]:
        """Busca todos os proxies da API Webshare v2 (paginada).
        Prioriza proxies HTTP — SOCKS5 é bloqueado por portais gov.br (eSocial, RF, TST).
        """
        entries: list[ProxyEntry] = []
        page = 1
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                r = await client.get(
                    "https://proxy.webshare.io/api/v2/proxy/list/",
                    headers={"Authorization": f"Token {self._api_key}"},
                    params={"mode": "direct", "page": page, "page_size": 100},
                )
                if r.status_code != 200:
                    break
                data = r.json()
                for p in data.get("results", []):
                    # Detecta tipo real do proxy — Webshare entrega tanto HTTP quanto SOCKS5
                    proxy_type = (p.get("proxy_type") or "http").lower()
                    if "socks" in proxy_type:
                        scheme = "socks5"
                    else:
                        scheme = "http"
                    url = (
                        f"{scheme}://{p['username']}:{p['password']}"
                        f"@{p['proxy_address']}:{p['port']}"
                    )
                    entries.append(ProxyEntry(
                        url=url,
                        country_code=(p.get("country_code") or "").upper(),
                    ))
                if not data.get("next"):
                    break
                page += 1
        # Prioriza HTTP sobre SOCKS5 (portais gov.br bloqueiam SOCKS)
        http_only = [e for e in entries if e.url.startswith("http://")]
        return http_only if http_only else entries

    async def _ensure_loaded(self) -> None:
        now = time.time()
        if self._proxies and now - self._last_refresh < self.REFRESH_INTERVAL_S:
            return
        async with self._lock:
            # Double-check após adquirir lock
            if self._proxies and now - self._last_refresh < self.REFRESH_INTERVAL_S:
                return

            if self._api_key:
                try:
                    fresh = await self._fetch_webshare()
                    if fresh:
                        # Preserva métricas para proxies já conhecidos
                        existing = {p.url: p for p in self._proxies}
                        merged: list[ProxyEntry] = []
                        for p in fresh:
                            if p.url in existing:
                                old = existing[p.url]
                                p.failures = old.failures
                                p.successes = old.successes
                                p.total_response_ms = old.total_response_ms
                                p.cooling_until = old.cooling_until
                            merged.append(p)
                        self._proxies = merged
                        self._last_refresh = time.time()
                        return
                except Exception:
                    pass  # Fallback para proxy estático abaixo

            # Proxy estático via PROXY_RESIDENCIAL_URL
            # Normaliza URL: alguns provedores entregam socks5 com prefixo http://
            # Detectar isso é impossível sem testar, então usamos como está.
            if self._static_url and not self._proxies:
                static = self._static_url.strip()
                # Se o usuário configurou http:// mas porta 6673/1080/4145 (SOCKS típico), avisa no log
                self._proxies = [ProxyEntry(url=static, country_code="")]
                self._last_refresh = time.time()

    # ── Seleção ─────────────────────────────────────────────────────────────

    def _pick(self, prefer_brazil: bool) -> Optional[ProxyEntry]:
        available = [p for p in self._proxies if p.is_available]
        if not available:
            # Todos em cooling → usa o que sai primeiro
            available = sorted(self._proxies, key=lambda p: p.cooling_until)[:1]
        if not available:
            return None

        if prefer_brazil:
            br = [p for p in available if p.country_code == "BR"]
            if br:
                available = br

        # Ordena: menor taxa de falha → menor latência → menos usado recentemente
        available.sort(key=lambda p: (p.failure_rate, p.avg_response_ms, p.last_used))
        # Escolhe aleatoriamente entre os 3 melhores para distribuir carga
        return random.choice(available[:3])

    async def get_proxy(self, prefer_brazil: bool = True) -> Optional[str]:
        """Retorna URL de proxy (http://user:pass@host:port) ou None se não configurado."""
        await self._ensure_loaded()
        entry = self._pick(prefer_brazil)
        return entry.url if entry else None

    # ── Métricas ─────────────────────────────────────────────────────────────

    def record_success(self, proxy_url: str, elapsed_ms: float) -> None:
        self._consecutive_failures[proxy_url] = 0
        for p in self._proxies:
            if p.url == proxy_url:
                p.successes += 1
                p.total_response_ms += elapsed_ms
                p.last_used = time.time()
                return

    def record_failure(self, proxy_url: str) -> None:
        consec = self._consecutive_failures.get(proxy_url, 0) + 1
        self._consecutive_failures[proxy_url] = consec
        for p in self._proxies:
            if p.url == proxy_url:
                p.failures += 1
                p.last_used = time.time()
                if consec >= self.MAX_CONSECUTIVE_FAILURES:
                    p.cooling_until = time.time() + self.COOLING_PERIOD_S
                return

    def stats(self) -> list[dict]:
        """Retorna métricas — credenciais ocultadas (só host:porta)."""
        return [
            {
                "url": p.url.split("@")[-1],  # ex: "200.1.2.3:8080"
                "country": p.country_code,
                "successes": p.successes,
                "failures": p.failures,
                "avg_ms": round(p.avg_response_ms),
                "cooling": p.cooling_until > time.time(),
                "available": p.is_available,
            }
            for p in self._proxies
        ]


# ── Singleton ────────────────────────────────────────────────────────────────

_manager: Optional[ProxyManager] = None


def init_proxy_manager(api_key: str, static_url: str = "") -> ProxyManager:
    """Inicializa o singleton. Chamar no lifespan da FastAPI."""
    global _manager
    _manager = ProxyManager(api_key=api_key, static_url=static_url)
    return _manager


def get_proxy_manager() -> Optional[ProxyManager]:
    """Retorna o singleton ou None se não inicializado."""
    return _manager
