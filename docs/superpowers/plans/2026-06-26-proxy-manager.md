# ProxyManager — Implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Criar um `ProxyManager` que gerencia pool Webshare com rotação automática, preferência por IPs brasileiros e monitoramento de latência, e obrigar toda consulta ao e-CAC a passar por ele.

**Architecture:** Um módulo `proxy_manager.py` expõe uma instância singleton inicializada no startup da FastAPI. Os lançadores Playwright (`_run_playwright_multi` e `_run_playwright_ecac_nss`) recebem um parâmetro `proxy_url` opcional. Um wrapper `_run_ecac_com_proxy` busca proxy do manager, executa a tarefa, registra sucesso/falha e faz retry com proxy diferente em caso de falha de rede.

**Tech Stack:** Python 3.11+, httpx (já instalado), Playwright (já instalado), FastAPI lifespan, variáveis de ambiente Railway.

## Global Constraints

- Credenciais (Webshare API key, senha de proxy) NUNCA em código-fonte — apenas env vars `WEBSHARE_API_KEY` e `PROXY_RESIDENCIAL_URL`
- Compatível com Railway (Linux, sem acesso ao sistema de arquivos persistente entre deploys)
- Não quebrar nenhum endpoint existente — o proxy é transparente se não configurado
- Sem dependências novas além das já no projeto (`httpx`, `playwright`)
- Commits em português (padrão do projeto)

---

## Mapa de Arquivos

| Arquivo | Ação | Responsabilidade |
|---------|------|-----------------|
| `backend/proxy_manager.py` | **Criar** | Pool, rotação, métricas, singleton |
| `backend/config.py` | **Modificar** | Adicionar `WEBSHARE_API_KEY: str = ""` |
| `backend/main.py` | **Modificar** | Chamar `init_proxy_manager()` no lifespan |
| `backend/routers/integracoes_router.py` | **Modificar** | Usar ProxyManager em e-CAC e FGTS |
| `tests/test_proxy_manager.py` | **Criar** | Testes unitários do ProxyManager |

---

## Task 1: ProxyManager — núcleo

**Files:**
- Create: `limio/backend/proxy_manager.py`
- Create: `limio/tests/test_proxy_manager.py`

**Interfaces:**
- Produces:
  - `class ProxyEntry` — dataclass com `url`, `country_code`, `failures`, `successes`, `total_response_ms`, `cooling_until`
  - `class ProxyManager(api_key, static_url)` com métodos:
    - `async get_proxy(prefer_brazil=True) -> str | None`
    - `record_success(proxy_url: str, elapsed_ms: float) -> None`
    - `record_failure(proxy_url: str) -> None`
    - `stats() -> list[dict]`
  - `init_proxy_manager(api_key: str, static_url: str = "") -> ProxyManager`
  - `get_proxy_manager() -> ProxyManager | None`

- [ ] **Step 1: Criar arquivo de teste com casos básicos**

```python
# limio/tests/test_proxy_manager.py
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
    m._last_refresh = time.time()  # evita fetch
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
```

- [ ] **Step 2: Executar testes (devem falhar — módulo não existe)**

```bash
cd limio && python -m pytest tests/test_proxy_manager.py -v 2>&1 | head -20
```
Esperado: `ModuleNotFoundError: No module named 'backend.proxy_manager'`

- [ ] **Step 3: Implementar `proxy_manager.py`**

```python
# limio/backend/proxy_manager.py
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
    url: str                    # http://user:pass@host:port
    country_code: str           # "BR", "US", "" …
    failures: int = 0
    successes: int = 0
    total_response_ms: float = 0.0
    last_used: float = 0.0
    cooling_until: float = 0.0  # epoch — quando o cooling-off termina

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
        """Busca todos os proxies da API Webshare v2 (paginada)."""
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
                    url = (
                        f"http://{p['username']}:{p['password']}"
                        f"@{p['proxy_address']}:{p['port']}"
                    )
                    entries.append(ProxyEntry(
                        url=url,
                        country_code=(p.get("country_code") or "").upper(),
                    ))
                if not data.get("next"):
                    break
                page += 1
        return entries

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
            if self._static_url and not self._proxies:
                self._proxies = [ProxyEntry(url=self._static_url, country_code="")]
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
```

- [ ] **Step 4: Executar testes (devem passar)**

```bash
cd limio && python -m pytest tests/test_proxy_manager.py -v
```
Esperado: 7 testes PASSED

- [ ] **Step 5: Commit**

```bash
cd limio
git add backend/proxy_manager.py tests/test_proxy_manager.py
git commit -m "feat: adiciona ProxyManager com pool Webshare e rotação automática"
```

---

## Task 2: Config + inicialização no startup

**Files:**
- Modify: `limio/backend/config.py`
- Modify: `limio/backend/main.py`

**Interfaces:**
- Consumes: `init_proxy_manager(api_key, static_url)` de `proxy_manager.py`
- Produces: `settings.WEBSHARE_API_KEY` disponível; `get_proxy_manager()` retorna instância após startup

- [ ] **Step 1: Adicionar WEBSHARE_API_KEY ao config**

Em `limio/backend/config.py`, dentro da classe `Settings`, adicionar após `PROXY_RESIDENCIAL_URL`:

```python
    # Webshare.io — pool de proxies residenciais com rotação automática
    # Configure em Railway → Variables: WEBSHARE_API_KEY=sua_chave
    WEBSHARE_API_KEY: str = ""
```

- [ ] **Step 2: Inicializar ProxyManager no lifespan**

Em `limio/backend/main.py`, adicionar import e inicialização:

```python
# Imports existentes mantidos. Adicionar:
from .proxy_manager import init_proxy_manager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    # Inicializa pool de proxies (Webshare API key + fallback estático)
    from .config import settings as _cfg
    init_proxy_manager(
        api_key=_cfg.WEBSHARE_API_KEY,
        static_url=_cfg.PROXY_RESIDENCIAL_URL,
    )
    yield
    stop_scheduler()
```

- [ ] **Step 3: Verificar startup sem erros**

```bash
cd limio && python -m uvicorn backend.main:app --port 8001 --timeout-keep-alive 1 &
sleep 3 && curl -s http://localhost:8001/api/auth/me -H "Authorization: Bearer x" | python -m json.tool
kill %1
```
Esperado: resposta JSON (mesmo que 401) sem `ImportError`

- [ ] **Step 4: Commit**

```bash
cd limio
git add backend/config.py backend/main.py
git commit -m "feat: inicializa ProxyManager no startup com WEBSHARE_API_KEY"
```

---

## Task 3: Suporte a proxy nos lançadores Playwright

**Files:**
- Modify: `limio/backend/routers/integracoes_router.py` — funções `_run_playwright_multi` e `_run_playwright_ecac_nss`

**Interfaces:**
- Consumes: nada novo — apenas adiciona parâmetro `proxy_url: str | None = None` às duas funções existentes
- Produces:
  - `_run_playwright_multi(..., proxy_url=None)` — passa proxy no contexto Playwright quando fornecido
  - `_run_playwright_ecac_nss(..., proxy_url=None)` — idem

**Nota:** Quando `proxy_url` é fornecido, usar `_CHROMIUM_ARGS_SEM_PROXY_FLAG` (sem `--no-proxy-server`). Quando não, manter comportamento atual com `_CHROMIUM_ARGS`.

- [ ] **Step 1: Atualizar `_run_playwright_multi`**

Localizar a função `_run_playwright_multi` (≈ linha 670) e substituir pela versão abaixo:

```python
async def _run_playwright_multi(
    cert_pem: bytes,
    key_pem: bytes,
    origins: list[str],
    tarefa_fn,
    extra_chromium_args: list[str] | None = None,
    proxy_url: str | None = None,
) -> dict:
    """Lança Playwright com client certificate em múltiplas origens.
    proxy_url: http://user:pass@host:port — quando fornecido, roteia pelo proxy.
    extra_chromium_args: flags adicionais (ex: --host-resolver-rules).
    """
    from playwright.async_api import async_playwright

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as cf:
        cf.write(cert_pem); cert_path = cf.name
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as kf:
        kf.write(key_pem); key_path = kf.name

    try:
        base_args = _CHROMIUM_ARGS_SEM_PROXY_FLAG if proxy_url else _CHROMIUM_ARGS
        args = list(base_args) + (extra_chromium_args or [])
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=args,
                env=_env_sem_proxy(),
            )
            ctx_kwargs: dict = dict(
                ignore_https_errors=True,
                user_agent=_UA,
                client_certificates=[
                    {"origin": o, "certPath": cert_path, "keyPath": key_path}
                    for o in origins
                ],
            )
            if proxy_url:
                ctx_kwargs["proxy"] = {"server": proxy_url}
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            try:
                return await tarefa_fn(page, context)
            finally:
                await context.close()
                await browser.close()
    finally:
        for p in (cert_path, key_path):
            try:
                os.unlink(p)
            except Exception:
                pass
```

- [ ] **Step 2: Atualizar `_run_playwright_ecac_nss`**

Localizar `_run_playwright_ecac_nss` (≈ linha 760) e adicionar parâmetro `proxy_url`:

```python
async def _run_playwright_ecac_nss(
    pfx_path: str,
    pfx_senha: str,
    tarefa_fn,
    proxy_url: str | None = None,
) -> dict:
    """Lança Playwright com certificado importado no NSS isolado.
    proxy_url: quando fornecido, roteia pelo proxy residencial.
    """
    import shutil
    from playwright.async_api import async_playwright

    tmp_home = tempfile.mkdtemp(prefix="ecac_nss_")
    try:
        ok, _ = await _importar_cert_pfx_nss_em(pfx_path, pfx_senha, tmp_home)
        if not ok:
            raise RuntimeError(
                "Falha ao importar certificado no banco NSS. "
                "Verifique se libnss3-tools está instalado na imagem do Railway."
            )

        env = _env_sem_proxy()
        env["HOME"] = tmp_home

        base_args = _CHROMIUM_ARGS_SEM_PROXY_FLAG if proxy_url else [
            a for a in _CHROMIUM_ARGS if "proxy" not in a.lower()
        ]

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=base_args,
                env=env,
            )
            ctx_kwargs: dict = dict(
                ignore_https_errors=True,
                user_agent=_UA,
            )
            if proxy_url:
                ctx_kwargs["proxy"] = {"server": proxy_url}
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            try:
                return await tarefa_fn(page, context)
            finally:
                await context.close()
                await browser.close()
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)
```

- [ ] **Step 3: Verificar sintaxe**

```bash
cd limio && python -c "from backend.routers import integracoes_router; print('OK')"
```
Esperado: `OK`

- [ ] **Step 4: Commit**

```bash
cd limio
git add backend/routers/integracoes_router.py
git commit -m "feat: lançadores Playwright aceitam proxy_url opcional"
```

---

## Task 4: Wrapper e-CAC com rotação de proxy

**Files:**
- Modify: `limio/backend/routers/integracoes_router.py` — adicionar função `_run_ecac_com_proxy` e atualizar chamadas em `buscar_pgdas`, `buscar_esocial`, `_consultar_cnd_fgts`

**Interfaces:**
- Consumes:
  - `get_proxy_manager()` de `proxy_manager`
  - `_run_playwright_multi(..., proxy_url=str)` — Task 3
  - `_run_playwright_ecac_nss(..., proxy_url=str)` — Task 3
- Produces: lógica centralizada de retry com rotação

- [ ] **Step 1: Adicionar import do ProxyManager no topo do router**

Logo após os imports existentes em `integracoes_router.py`:

```python
from ..proxy_manager import get_proxy_manager
```

- [ ] **Step 2: Adicionar função `_run_ecac_com_proxy` antes dos endpoints**

Inserir a função após a definição de `_run_playwright_ecac_nss` (≈ linha 820):

```python
async def _run_ecac_com_proxy(
    *,
    tarefa_fn,
    # Modo NSS (e-CAC com seletor GOV.BR)
    pfx_path: str | None = None,
    pfx_senha: str | None = None,
    # Modo client_certificates (fallback)
    cert_pem: bytes | None = None,
    key_pem: bytes | None = None,
    origins: list[str] | None = None,
    extra_chromium_args: list[str] | None = None,
    prefer_brazil: bool = True,
    max_tentativas: int = 3,
) -> dict:
    """Executa tarefa e-CAC roteando pelo ProxyManager.
    Tenta até max_tentativas proxies distintos em caso de falha de rede.
    Se ProxyManager não estiver configurado, executa sem proxy.
    """
    import time as _time

    manager = get_proxy_manager()
    proxies_tentados: set[str] = set()

    for tentativa in range(max_tentativas):
        proxy_url: str | None = None
        if manager:
            proxy_url = await manager.get_proxy(prefer_brazil=prefer_brazil)
            # Evita retentar o mesmo proxy numa mesma rodada
            if proxy_url in proxies_tentados:
                proxy_url = None
            if proxy_url:
                proxies_tentados.add(proxy_url)

        t0 = _time.monotonic()
        try:
            if pfx_path and pfx_senha is not None:
                resultado = await _run_playwright_ecac_nss(
                    pfx_path, pfx_senha, tarefa_fn, proxy_url=proxy_url
                )
            else:
                resultado = await _run_playwright_multi(
                    cert_pem, key_pem,
                    origins or [],
                    tarefa_fn,
                    extra_chromium_args=extra_chromium_args,
                    proxy_url=proxy_url,
                )
            elapsed_ms = (_time.monotonic() - t0) * 1000
            if proxy_url and manager:
                manager.record_success(proxy_url, elapsed_ms)
            return resultado

        except Exception as e:
            elapsed_ms = (_time.monotonic() - t0) * 1000
            if proxy_url and manager:
                manager.record_failure(proxy_url)

            is_last = tentativa == max_tentativas - 1
            if is_last:
                raise
            # Falha de rede: tenta próximo proxy
            err_str = str(e).lower()
            if not any(k in err_str for k in ("net::", "connection", "proxy", "timeout", "socks")):
                raise  # Erro de lógica/portal — não adianta trocar proxy

    raise RuntimeError("Todas as tentativas de proxy falharam")  # nunca alcançado
```

- [ ] **Step 3: Atualizar `buscar_pgdas` para usar `_run_ecac_com_proxy`**

Localizar o bloco `try:` dentro de `buscar_pgdas` (≈ linha 66) e substituir:

```python
    try:
        if usar_procuracao:
            pfx_path = getattr(escritorio, "cert_procuracao_path", None)
            pfx_senha = getattr(escritorio, "cert_procuracao_senha", "") or ""
            if pfx_path and os.path.exists(pfx_path):
                resultado = await _run_ecac_com_proxy(
                    tarefa_fn=lambda p, c: _tarefa_pgdas(p, c, cliente.cnpj, ano, usar_procuracao=True),
                    pfx_path=pfx_path,
                    pfx_senha=pfx_senha,
                    prefer_brazil=True,
                )
            else:
                resultado = await _run_ecac_com_proxy(
                    tarefa_fn=lambda p, c: _tarefa_pgdas(p, c, cliente.cnpj, ano, usar_procuracao=True),
                    cert_pem=cert_pem,
                    key_pem=key_pem,
                    origins=["https://cav.receita.fazenda.gov.br", "https://acesso.gov.br",
                             "https://sso.acesso.gov.br"],
                    prefer_brazil=True,
                )
        else:
            resultado = await _run_ecac_com_proxy(
                tarefa_fn=lambda p, c: _tarefa_pgdas(p, c, cliente.cnpj, ano, usar_procuracao=False),
                cert_pem=cert_pem,
                key_pem=key_pem,
                origins=["https://cav.receita.fazenda.gov.br", "https://acesso.gov.br"],
                prefer_brazil=True,
            )
        for rec in resultado.get("receitas", []):
            await _upsert_receita(db, escritorio.id, cliente.id, rec["competencia"], rec["valor_receita"], "pgdas_d")
        await db.commit()
        return {"status": "ok", **resultado}
    except Exception as e:
        _raise_http(e)
```

- [ ] **Step 4: Atualizar `buscar_esocial` para usar `_run_ecac_com_proxy`**

Localizar o bloco `try:` dentro de `buscar_esocial` (≈ linha 115) e substituir:

```python
    try:
        esocial_ip = await _resolver_doh("empregador.esocial.gov.br")
        extra_args: list[str] = []
        if esocial_ip:
            extra_args.append(f"--host-resolver-rules=MAP empregador.esocial.gov.br {esocial_ip}")

        resultado = await _run_ecac_com_proxy(
            tarefa_fn=lambda p, c: _tarefa_esocial(p, c, cliente.cnpj, ano, usar_procuracao),
            cert_pem=cert_pem,
            key_pem=key_pem,
            origins=["https://empregador.esocial.gov.br", "https://www.esocial.gov.br",
                     "https://login.esocial.gov.br", "https://acesso.gov.br",
                     "https://sso.acesso.gov.br"],
            extra_chromium_args=extra_args,
            prefer_brazil=True,
        )
        for f in resultado.get("folhas", []):
            await _upsert_folha(db, escritorio.id, cliente.id, f["competencia"], f["valor_total_folha"])
        await db.commit()
        return {"status": "ok", **resultado}
    except Exception as e:
        _raise_http(e)
```

- [ ] **Step 5: Atualizar `_consultar_cnd_fgts` para usar ProxyManager**

Localizar a seção de proxy em `_consultar_cnd_fgts` (≈ linha 1900) e substituir o bloco `if proxy_url:` por:

```python
    manager = get_proxy_manager()
    proxy_url: str | None = None
    if manager:
        proxy_url = await manager.get_proxy(prefer_brazil=True)
    # Fallback: PROXY_RESIDENCIAL_URL direto (se manager não carregou)
    if not proxy_url:
        from ..config import settings as _cfg_fgts
        proxy_url = _cfg_fgts.PROXY_RESIDENCIAL_URL or None

    import time as _t_fgts
    t0_fgts = _t_fgts.monotonic()
    try:
        if proxy_url:
            from playwright.async_api import async_playwright as _apw_fgts
            async with _apw_fgts() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=_CHROMIUM_ARGS_SEM_PROXY_FLAG,
                    env=_env_sem_proxy(),
                )
                context = await browser.new_context(
                    proxy={"server": proxy_url},
                    ignore_https_errors=True,
                    user_agent=_UA,
                )
                page = await context.new_page()
                try:
                    resultado_fgts = await _tarefa_fgts(page)
                finally:
                    await context.close()
                    await browser.close()
        else:
            resultado_fgts = await _run_playwright_no_cert(_tarefa_fgts)

        elapsed_ms_fgts = (_t_fgts.monotonic() - t0_fgts) * 1000
        if proxy_url and manager:
            manager.record_success(proxy_url, elapsed_ms_fgts)
        return resultado_fgts

    except Exception:
        if proxy_url and manager:
            manager.record_failure(proxy_url)
        raise
```

- [ ] **Step 6: Verificar sintaxe**

```bash
cd limio && python -c "from backend.routers import integracoes_router; print('OK')"
```
Esperado: `OK`

- [ ] **Step 7: Commit**

```bash
cd limio
git add backend/routers/integracoes_router.py
git commit -m "feat: toda consulta e-CAC/eSocial/FGTS passa pelo ProxyManager"
```

---

## Task 5: Endpoint de diagnóstico de proxies

**Files:**
- Modify: `limio/backend/routers/integracoes_router.py` — adicionar endpoint `/api/integracoes/proxy-stats`

**Interfaces:**
- Consumes: `get_proxy_manager().stats()` — Task 1
- Produces: `GET /api/integracoes/proxy-stats` retorna lista de métricas por proxy

- [ ] **Step 1: Adicionar endpoint após `diagnostico_rede`**

Inserir após o endpoint `diagnostico_rede` (≈ linha 280):

```python
@router.get("/proxy-stats")
async def proxy_stats(
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """Retorna métricas do pool de proxies — host:porta, país, latência, falhas.
    Credenciais (usuário/senha) são ocultadas na resposta.
    """
    manager = get_proxy_manager()
    if not manager:
        return {"status": "não inicializado", "proxies": []}
    stats = manager.stats()
    return {
        "status": "ok",
        "total": len(stats),
        "disponiveis": sum(1 for p in stats if p["available"]),
        "em_cooling": sum(1 for p in stats if p["cooling"]),
        "proxies": stats,
    }
```

- [ ] **Step 2: Testar endpoint manualmente**

```bash
# Obter token primeiro
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"amarcellalima@gmail.com","senha":"sua_senha"}' | python -m json.tool | grep access_token | cut -d'"' -f4)

curl -s http://localhost:8000/api/integracoes/proxy-stats \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool
```
Esperado: JSON com `"status": "ok"` e `"proxies": []` (ou lista se WEBSHARE_API_KEY configurada)

- [ ] **Step 3: Commit e push**

```bash
cd limio
git add backend/routers/integracoes_router.py
git commit -m "feat: endpoint /proxy-stats expõe métricas do pool de proxies"
git push origin main
```

---

## Configuração Railway

Após o push, adicionar as variáveis no Railway:

| Variável | Valor | Onde obter |
|----------|-------|-----------|
| `WEBSHARE_API_KEY` | `sua-chave` | webshare.io → API Keys |
| `PROXY_RESIDENCIAL_URL` | `http://user:pass@host:port` | webshare.io → Proxy credentials (fallback se API key falhar) |

**ATENÇÃO:** Nunca colocar esses valores em código ou commitar no repositório.

---

## Self-Review

**Cobertura do spec:**
- ✅ ProxyManager com pool Webshare
- ✅ Rotação automática em falha (cooling + retry)
- ✅ Monitoramento de tempo de resposta (`record_success` com `elapsed_ms`)
- ✅ Preferência por IPs brasileiros (`prefer_brazil=True`)
- ✅ Toda consulta e-CAC passa pelo manager (`_run_ecac_com_proxy`)
- ✅ Credenciais apenas em env vars (`WEBSHARE_API_KEY`, `PROXY_RESIDENCIAL_URL`)
- ✅ Endpoint de diagnóstico (`/proxy-stats`)

**Placeholders:** nenhum — todo código está completo.

**Consistência de tipos:**
- `get_proxy() -> str | None` ✅ usado como `proxy_url: str | None` nas Tasks 3 e 4
- `record_success(proxy_url: str, elapsed_ms: float)` ✅ chamado com `elapsed_ms = (monotonic() - t0) * 1000`
- `_run_playwright_multi(..., proxy_url=None)` ✅ assinatura definida Task 3, usada Task 4
- `_run_playwright_ecac_nss(..., proxy_url=None)` ✅ idem
