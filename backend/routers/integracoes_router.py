"""
Integrações com Receita Federal (PGDAS-D / e-CAC) e eSocial via certificado A1.

Estratégia:
  - PGDAS-D / Simples: Playwright com client certificate
  - eSocial: Playwright no portal empregador.esocial.gov.br
  - Certidões (CND Federal, FGTS, CNDT): httpx + Playwright conforme portal
  - Lucro Presumido/Real faturamento: e-CAC > Consulta de Receita/DCTF

O Chromium usa o certificado A1 diretamente para autenticar, exatamente como o
contador faria manualmente no browser, sem precisar de webservice especializado.
"""
from __future__ import annotations

import asyncio
import re
import tempfile
import os
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from ..models import Cliente, Escritorio, ReceitaHistorica, FolhaMensal
from ..auth import get_escritorio_atual
from ..proxy_manager import get_proxy_manager

router = APIRouter(prefix="/api/integracoes", tags=["Integrações RF/eSocial"])

_PGDAS_TIMEOUT = 60_000   # ms por operação no Playwright
_ESOCIAL_TIMEOUT = 60_000


# ─── Endpoints individuais ────────────────────────────────────────────────────

@router.post("/{cliente_id}/buscar-pgdas")
async def buscar_pgdas(
    cliente_id: int,
    ano: int = Query(default=2025),
    senha: str = Query(default=""),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Busca receita bruta no PGDAS-D.
    Prioriza certificado de procuração do escritório (acesso via e-CAC).
    Fallback para certificado próprio do cliente.
    """
    cliente = await _get_cliente(cliente_id, escritorio.id, db)

    if not await _playwright_ok():
        raise HTTPException(503, "Módulo de automação não disponível.")

    # Tenta certificado de procuração do escritório (acesso único via e-CAC)
    usar_procuracao = bool(getattr(escritorio, "cert_procuracao_path", None))
    if usar_procuracao:
        try:
            cert_pem, key_pem = await _cert_escritorio_ou_erro(escritorio)
        except HTTPException:
            usar_procuracao = False

    if not usar_procuracao:
        cert_pem, key_pem = await _cert_ou_erro(cliente, senha)

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
                    origins=_ECAC_ORIGINS,
                    prefer_brazil=True,
                )
        else:
            resultado = await _run_ecac_com_proxy(
                tarefa_fn=lambda p, c: _tarefa_pgdas(p, c, cliente.cnpj, ano, usar_procuracao=False),
                cert_pem=cert_pem,
                key_pem=key_pem,
                origins=_ECAC_ORIGINS,
                prefer_brazil=True,
            )
        for rec in resultado.get("receitas", []):
            await _upsert_receita(db, escritorio.id, cliente.id, rec["competencia"], rec["valor_receita"], "pgdas_d")
        await db.commit()
        return {"status": "ok", **resultado}
    except Exception as e:
        _raise_http(e)


@router.post("/{cliente_id}/buscar-esocial")
async def buscar_esocial(
    cliente_id: int,
    ano: int = Query(default=2025),
    senha: str = Query(default=""),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Busca dados de folha no eSocial.
    Prioriza certificado de procuração do escritório.
    Fallback para certificado próprio do cliente.
    """
    cliente = await _get_cliente(cliente_id, escritorio.id, db)

    if not await _playwright_ok():
        raise HTTPException(503, "Módulo de automação não disponível.")

    # Tenta certificado de procuração do escritório
    usar_procuracao = bool(getattr(escritorio, "cert_procuracao_path", None))
    if usar_procuracao:
        try:
            cert_pem, key_pem = await _cert_escritorio_ou_erro(escritorio)
        except HTTPException:
            usar_procuracao = False

    if not usar_procuracao:
        cert_pem, key_pem = await _cert_ou_erro(cliente, senha)

    try:
        # Usa _run_ecac_com_proxy (pool rotativo Webshare + retry) igual ao PGDAS.
        # O IP estático de PROXY_RESIDENCIAL_URL é frequentemente bloqueado pelo eSocial.
        resultado = await _run_ecac_com_proxy(
            tarefa_fn=lambda p, c: _tarefa_esocial(p, c, cliente.cnpj, ano, usar_procuracao),
            cert_pem=cert_pem,
            key_pem=key_pem,
            origins=_ESOCIAL_ORIGINS,
            prefer_brazil=True,
            max_tentativas=3,
        )
        for f in resultado.get("folhas", []):
            await _upsert_folha(db, escritorio.id, cliente.id, f["competencia"], f["valor_total_folha"])
        await db.commit()
        return {"status": "ok", **resultado}
    except Exception as e:
        _raise_http(e)


@router.post("/{cliente_id}/buscar-faturamento-lp")
async def buscar_faturamento_lp(
    cliente_id: int,
    ano: int = Query(default=2025),
    senha: str = Query(default=""),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """
    Busca faturamento de empresa LP/LR via EFD Contribuições (e-CAC / SPED).
    Navega para e-CAC → SPED → EFD-Contribuições e extrai a receita bruta PIS/COFINS.
    """
    cliente = await _get_cliente(cliente_id, escritorio.id, db)
    cert_pem, key_pem = await _cert_ou_erro(cliente, senha)

    if not await _playwright_ok():
        raise HTTPException(503, "Módulo de automação não disponível.")

    try:
        resultado = await _run_playwright_multi(
            cert_pem, key_pem,
            ["https://cav.receita.fazenda.gov.br", "https://sped.rfb.gov.br",
             "https://www.receita.fazenda.gov.br"],
            lambda p, c: _tarefa_efd_contribuicoes(p, c, cliente.cnpj, ano),
        )
        for rec in resultado.get("receitas", []):
            await _upsert_receita(db, escritorio.id, cliente.id, rec["competencia"], rec["valor_receita"], "efd_contrib")
        await db.commit()
        return {"status": "ok", **resultado}
    except Exception as e:
        _raise_http(e)


# ─── Busca em lote ────────────────────────────────────────────────────────────

@router.post("/buscar-lote")
async def buscar_lote(
    payload: dict,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """
    Busca PGDAS-D e eSocial em lote para todos os clientes com certificado.
    Payload: { "ano": 2025, "cliente_ids": [1,2,3] (opcional) }
    """
    ano = int(payload.get("ano", date.today().year))
    ids_filtro = payload.get("cliente_ids")

    q = select(Cliente).where(Cliente.escritorio_id == escritorio.id, Cliente.ativo == True)
    if ids_filtro:
        q = q.where(Cliente.id.in_(ids_filtro))
    clientes = (await db.execute(q)).scalars().all()

    resultados = []
    for cliente in clientes:
        item: dict = {"cliente_id": cliente.id, "razao_social": cliente.razao_social,
                      "pgdas": None, "esocial": None, "erro": None}
        try:
            cert_pem, key_pem, err = await _carregar_certificado(cliente, "nfse")
            if err:
                cert_pem, key_pem, err = await _carregar_certificado(cliente, "nfe")
            if err:
                item["erro"] = err
                resultados.append(item)
                continue

            if await _playwright_ok():
                try:
                    res = await _run_playwright(cert_pem, key_pem,
                        "https://www8.receita.fazenda.gov.br",
                        lambda p, c: _tarefa_pgdas(p, c, cliente.cnpj, ano))
                    for rec in res.get("receitas", []):
                        await _upsert_receita(db, escritorio.id, cliente.id,
                                              rec["competencia"], rec["valor_receita"], "pgdas_d")
                    await db.commit()
                    item["pgdas"] = {"competencias": len(res.get("receitas", [])), "aviso": res.get("aviso")}
                except Exception as e:
                    item["pgdas"] = {"erro": str(e)}

                try:
                    res = await _run_playwright(cert_pem, key_pem,
                        "https://empregador.esocial.gov.br",
                        lambda p, c: _tarefa_esocial(p, c, cliente.cnpj, ano))
                    for f in res.get("folhas", []):
                        await _upsert_folha(db, escritorio.id, cliente.id,
                                            f["competencia"], f["valor_total_folha"])
                    await db.commit()
                    item["esocial"] = {"competencias": len(res.get("folhas", [])), "aviso": res.get("aviso")}
                except Exception as e:
                    item["esocial"] = {"erro": str(e)}
        except Exception as e:
            item["erro"] = str(e)
        resultados.append(item)

    return {
        "ano": ano,
        "total_clientes": len(resultados),
        "sucesso_pgdas": sum(1 for r in resultados if r.get("pgdas") and not r["pgdas"].get("erro")),
        "sucesso_esocial": sum(1 for r in resultados if r.get("esocial") and not r["esocial"].get("erro")),
        "resultados": resultados,
    }


# ─── Certidões automáticas ────────────────────────────────────────────────────

@router.get("/diagnostico-rede")
async def diagnostico_rede():
    """Testa conectividade com portais do governo via httpx (sem Playwright).
    Retorna status HTTP ou erro por URL — útil para diagnosticar bloqueios de rede.
    """
    import httpx as _hx
    urls = [
        "https://cav.receita.fazenda.gov.br/autenticacao/login",
        "https://empregador.esocial.gov.br/",
        "https://www.esocial.gov.br/",
        "https://www.tst.jus.br/certidao",
        "https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf",
        "https://www.sefaz.am.gov.br/portal/certidao-negativa",
    ]
    proxy_vars = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}
    resultados: dict = {"proxy_vars": proxy_vars, "urls": {}}
    for url in urls:
        try:
            async with _hx.AsyncClient(verify=False, timeout=12, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 Chrome/125.0.0.0"})
                resultados["urls"][url] = {
                    "ok": True, "status": r.status_code, "bytes": len(r.content),
                }
        except Exception as e:
            resultados["urls"][url] = {"ok": False, "erro": str(e)[:300]}
    return resultados


@router.get("/proxy-test")
async def proxy_test():
    """Diagnóstico completo do proxy: testa httpx sem proxy, httpx com proxy,
    e inspeciona ProxyManager. Não requer autenticação — só para diagnóstico.
    """
    import httpx as _hx
    from ..config import settings as _cfg

    proxy_url = os.environ.get("PROXY_RESIDENCIAL_URL") or _cfg.PROXY_RESIDENCIAL_URL or ""
    result: dict = {
        "proxy_residencial_url_configurado": bool(proxy_url),
        "proxy_residencial_prefix": (proxy_url[:30] + "...") if proxy_url else "VAZIO",
    }

    # 1. IP de saída do Railway sem proxy
    try:
        async with _hx.AsyncClient(timeout=10, verify=False) as c:
            r = await c.get("https://ipv4.webshare.io/")
            result["ip_railway_direto"] = r.text.strip()
    except Exception as e:
        result["ip_railway_direto"] = f"ERRO: {e!s:.200}"

    # 2. IP de saída com proxy via httpx
    if proxy_url:
        try:
            async with _hx.AsyncClient(proxy=proxy_url, timeout=15, verify=False) as c:
                r = await c.get("https://ipv4.webshare.io/")
                result["ip_via_proxy_httpx"] = r.text.strip()
        except Exception as e:
            result["ip_via_proxy_httpx"] = f"ERRO: {e!s:.200}"
    else:
        result["ip_via_proxy_httpx"] = "PROXY_RESIDENCIAL_URL não configurado"

    # 3. ProxyManager
    manager = get_proxy_manager()
    if manager:
        pm_proxy = await manager.get_proxy(prefer_brazil=True)
        stats = manager.stats()
        result["proxy_manager"] = {
            "inicializado": True,
            "total_proxies": len(stats),
            "proxy_selecionado_prefix": (pm_proxy[:30] + "...") if pm_proxy else "NENHUM",
        }
        # Testa o proxy do manager via httpx
        if pm_proxy:
            try:
                async with _hx.AsyncClient(proxy=pm_proxy, timeout=15, verify=False) as c:
                    r = await c.get("https://ipv4.webshare.io/")
                    result["ip_via_proxy_manager_httpx"] = r.text.strip()
            except Exception as e:
                result["ip_via_proxy_manager_httpx"] = f"ERRO: {e!s:.200}"
    else:
        result["proxy_manager"] = {
            "inicializado": False,
            "motivo": "WEBSHARE_API_KEY não configurado ou vazio",
        }

    # 4. Playwright + proxy (testa se o Chromium consegue usar o proxy)
    if proxy_url and await _playwright_ok():
        try:
            from playwright.async_api import async_playwright
            parsed = _parse_proxy(proxy_url)
            launch_kw: dict = {
                "headless": True,
                "args": list(_CHROMIUM_ARGS_SEM_PROXY_FLAG),
                "env": _env_sem_proxy(),
                "proxy": parsed,
            }
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_kw)
                ctx = await browser.new_context(ignore_https_errors=True, user_agent=_UA)
                page = await ctx.new_page()
                try:
                    await page.goto("https://ipv4.webshare.io/", timeout=20_000)
                    ip_text = (await page.text_content("body") or "").strip()
                    result["ip_via_proxy_playwright"] = ip_text[:50]
                except Exception as e:
                    result["ip_via_proxy_playwright"] = f"ERRO: {e!s:.300}"
                finally:
                    await ctx.close()
                    await browser.close()
        except Exception as e:
            result["ip_via_proxy_playwright"] = f"ERRO (launch): {e!s:.300}"
    else:
        result["ip_via_proxy_playwright"] = "pulado (proxy não configurado ou playwright indisponível)"

    # 5. empregador.esocial.gov.br não existe mais em DNS (domínio desativado pelo SERPRO).
    # Testa os domínios corretos: login.esocial.gov.br e acesso.gov.br
    pm_proxy_esocial = None
    if manager:
        pm_proxy_esocial = await manager.get_proxy(prefer_brazil=True)
    proxy_para_esocial = pm_proxy_esocial or proxy_url

    for alvo_url, alvo_chave in [
        ("https://login.esocial.gov.br/login.aspx", "login_esocial"),
        ("https://sso.acesso.gov.br/", "sso_acesso_gov"),
    ]:
        # 5a. Direto via httpx
        try:
            async with _hx.AsyncClient(timeout=12, verify=False) as c:
                r = await c.get(alvo_url)
                result[f"{alvo_chave}_direto_httpx"] = f"status={r.status_code}"
        except Exception as e:
            result[f"{alvo_chave}_direto_httpx"] = f"ERRO: {e!s:.200}"
        # 5b. Via proxy
        if proxy_para_esocial:
            try:
                async with _hx.AsyncClient(proxy=proxy_para_esocial, timeout=12, verify=False) as c:
                    r = await c.get(alvo_url)
                    result[f"{alvo_chave}_proxy_httpx"] = f"status={r.status_code}"
            except Exception as e:
                result[f"{alvo_chave}_proxy_httpx"] = f"ERRO: {e!s:.200}"

    # 6. Playwright direto (sem proxy) para login.esocial.gov.br
    if await _playwright_ok():
        try:
            from playwright.async_api import async_playwright
            launch_kw_direto: dict = {
                "headless": True,
                "args": list(_CHROMIUM_ARGS),
                "env": _env_sem_proxy(),
            }
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_kw_direto)
                ctx = await browser.new_context(ignore_https_errors=True, user_agent=_UA)
                page = await ctx.new_page()
                try:
                    resp = await page.goto("https://login.esocial.gov.br/login.aspx", timeout=20_000)
                    result["login_esocial_direto_playwright"] = f"status={resp.status if resp else '??'}"
                except Exception as e:
                    result["login_esocial_direto_playwright"] = f"ERRO: {e!s:.300}"
                finally:
                    await ctx.close()
                    await browser.close()
        except Exception as e:
            result["login_esocial_direto_playwright"] = f"ERRO (launch): {e!s:.300}"
    else:
        result["login_esocial_direto_playwright"] = "playwright indisponível"

    return result


@router.get("/proxy-stats")
async def proxy_stats(
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """Métricas do pool de proxies: latência, falhas, cooling-off.
    Credenciais (usuário/senha) são ocultadas — exibe apenas host:porta.
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


@router.post("/certidao-preview")
async def preview_certidao(
    payload: dict,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """
    Consulta automática SEM salvar — retorna status, validade e número para preencher o modal.
    Payload: { "cliente_id": 3, "tipo": "cnd_federal" }
    """
    cliente_id = payload.get("cliente_id")
    tipo = payload.get("tipo", "")
    if not cliente_id or not tipo:
        raise HTTPException(400, "cliente_id e tipo são obrigatórios.")

    cliente_r = await db.execute(select(Cliente).where(
        Cliente.id == cliente_id, Cliente.escritorio_id == escritorio.id))
    cliente = cliente_r.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")

    cnpj = re.sub(r"\D", "", cliente.cnpj or "")
    if len(cnpj) != 14:
        raise HTTPException(400, "CNPJ do cliente inválido.")

    uf = (cliente.uf or "AM").upper()
    return await _executar_consulta_certidao(tipo, cnpj, uf, cliente)


@router.post("/certidao-consultar")
async def consultar_certidao_auto(
    payload: dict,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """
    Consulta automaticamente uma certidão no órgão competente.
    Payload: { "certidao_id": 5 }
    Atualiza status, data_validade e numero_certidao no banco.
    """
    from ..models import Certidao

    cid = payload.get("certidao_id")
    if not cid:
        raise HTTPException(400, "certidao_id obrigatório.")

    r = await db.execute(select(Certidao).where(
        Certidao.id == cid, Certidao.escritorio_id == escritorio.id))
    cert = r.scalar_one_or_none()
    if not cert:
        raise HTTPException(404, "Certidão não encontrada.")

    cliente_r = await db.execute(select(Cliente).where(Cliente.id == cert.cliente_id))
    cliente = cliente_r.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")

    cnpj = re.sub(r"\D", "", cliente.cnpj or "")
    if len(cnpj) != 14:
        raise HTTPException(400, "CNPJ do cliente inválido.")

    uf = (cliente.uf or "AM").upper()

    try:
        res = await _executar_consulta_certidao(cert.tipo, cnpj, uf, cliente)

        # Salva PDF retornado pelo Playwright (arquivo temporário)
        pdf_temp = res.pop("pdf_temp_path", None)
        if pdf_temp and os.path.exists(pdf_temp):
            try:
                from ..config import settings as _cfg
                import shutil as _shutil
                pasta_pdf = os.path.join(_cfg.DATA_DIR, "certidoes",
                                         str(escritorio.id), str(cert.cliente_id))
                os.makedirs(pasta_pdf, exist_ok=True)
                nome_pdf = f"{cert.tipo}_{date.today().isoformat()}.pdf"
                caminho_pdf = os.path.join(pasta_pdf, nome_pdf)
                _shutil.move(pdf_temp, caminho_pdf)
                cert.arquivo_path = caminho_pdf
            except Exception:
                pass

        # Atualiza certidão com os dados retornados
        cert.data_consulta = datetime.utcnow()
        cert.status = res.get("status", cert.status)
        if res.get("data_validade"):
            cert.data_validade = res["data_validade"]
        if res.get("numero_certidao"):
            cert.numero_certidao = res["numero_certidao"]
        if res.get("observacao"):
            cert.observacao = res["observacao"]
        cert.atualizado_em = datetime.utcnow()
        await db.commit()

        return {"ok": True, "resultado": res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Erro na consulta automática: {e}")


async def _executar_consulta_certidao(tipo: str, cnpj: str, uf: str, cliente: Cliente) -> dict:
    """Despacha a consulta para o órgão correto. Usado por preview e por certidao-consultar."""
    if tipo == "cnd_federal":
        return await _consultar_cnd_federal(cnpj)
    if tipo == "cnd_fgts":
        return await _consultar_cnd_fgts(cnpj, uf)
    if tipo == "cndt_tst":
        return await _consultar_cndt_tst(cnpj)
    if tipo == "cndt_trt":
        return await _consultar_cndt_trt(cnpj, uf)
    if tipo in ("cnd_estadual", "cnd_estadual_nc"):
        return await _consultar_cnd_estadual(cnpj, uf, tipo)
    if tipo == "cnd_municipal":
        return await _consultar_cnd_municipal(cnpj, cliente.municipio or "", uf)
    if tipo == "cnd_falencia":
        return await _consultar_cnd_falencia(cnpj, uf, cliente.razao_social or "")
    raise HTTPException(400, f"Tipo '{tipo}' ainda não suportado para consulta automática.")


# ─── Helpers genéricos ────────────────────────────────────────────────────────

async def _get_cliente(cliente_id: int, escritorio_id: int, db: AsyncSession) -> Cliente:
    r = await db.execute(select(Cliente).where(
        Cliente.id == cliente_id, Cliente.escritorio_id == escritorio_id))
    c = r.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Cliente não encontrado.")
    return c


async def _cert_ou_erro(cliente: Cliente, senha_override: str = "") -> tuple[bytes, bytes]:
    cert_pem, key_pem, err = await _carregar_certificado(cliente, "nfse", senha_override)
    if err:
        cert_pem, key_pem, err = await _carregar_certificado(cliente, "nfe", senha_override)
    if err:
        raise HTTPException(400, err)
    return cert_pem, key_pem


async def _cert_escritorio_ou_erro(escritorio) -> tuple[bytes, bytes]:
    """Carrega o certificado A1 do escritório (para acesso via procuração)."""
    path = getattr(escritorio, "cert_procuracao_path", None)
    senha = getattr(escritorio, "cert_procuracao_senha", None) or ""
    if not path:
        raise HTTPException(400, "Certificado de procuração do escritório não configurado. Acesse Configurações → Certificado de Procuração.")
    if not os.path.exists(path):
        raise HTTPException(400, "Arquivo do certificado de procuração não encontrado no servidor. Faça o upload novamente.")
    try:
        from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
        with open(path, "rb") as f:
            pfx = f.read()
        p12 = load_pkcs12(pfx, senha.encode() if senha else b"")
        cert_pem = p12.cert.certificate.public_bytes(Encoding.PEM)
        key_pem = p12.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        return cert_pem, key_pem
    except Exception as e:
        raise HTTPException(400, f"Erro ao abrir certificado de procuração: {e}")


async def _carregar_certificado(cliente: Cliente, tipo: str, senha_override: str = ""):
    path = getattr(cliente, f"{tipo}_certificado_path", None)
    senha = senha_override or getattr(cliente, f"{tipo}_certificado_senha", None) or ""

    if not path:
        return None, None, f"Certificado {tipo.upper()} não cadastrado."
    if not os.path.exists(path):
        return None, None, f"Arquivo do certificado {tipo.upper()} não encontrado no servidor."

    try:
        from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

        with open(path, "rb") as f:
            pfx = f.read()
        p12 = load_pkcs12(pfx, senha.encode() if senha else b"")
        cert_pem = p12.cert.certificate.public_bytes(Encoding.PEM)
        key_pem = p12.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        return cert_pem, key_pem, None
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ("mac", "password", "invalid", "decrypt")):
            return None, None, f"Senha incorreta para o certificado {tipo.upper()}."
        return None, None, f"Erro ao abrir certificado {tipo.upper()}: {e}"


async def _playwright_ok() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa
        return True
    except ImportError:
        return False


# ── 2captcha.com ─────────────────────────────────────────────────────────────

async def _resolver_recaptcha_2captcha(page_url: str, site_key: str) -> str | None:
    """Resolve reCAPTCHA v2 via 2captcha.com. Retorna token ou None."""
    import os, httpx as _hx
    from ..config import settings as _cfg
    api_key = os.environ.get("DOIS_CAPTCHA_KEY") or _cfg.DOIS_CAPTCHA_KEY
    if not api_key:
        return None
    try:
        async with _hx.AsyncClient(timeout=15) as c:
            r = await c.post("https://api.2captcha.com/createTask", json={
                "clientKey": api_key,
                "task": {
                    "type": "RecaptchaV2TaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                }
            })
            data = r.json()
            if data.get("errorId"):
                return None
            task_id = data["taskId"]

        # Poll até 120 segundos (reCAPTCHA v2 leva ~20–40s)
        for _ in range(24):
            await asyncio.sleep(5)
            async with _hx.AsyncClient(timeout=15) as c:
                r = await c.post("https://api.2captcha.com/getTaskResult", json={
                    "clientKey": api_key,
                    "taskId": task_id,
                })
                data = r.json()
                if data.get("status") == "ready":
                    return data["solution"]["gRecaptchaResponse"]
        return None
    except Exception:
        return None


async def _resolver_captcha_imagem_2captcha(img_bytes: bytes) -> str | None:
    """Resolve CAPTCHA de imagem via 2captcha.com. Retorna texto ou None."""
    import os, base64, httpx as _hx
    from ..config import settings as _cfg
    api_key = os.environ.get("DOIS_CAPTCHA_KEY") or _cfg.DOIS_CAPTCHA_KEY
    if not api_key:
        return None
    try:
        b64 = base64.b64encode(img_bytes).decode()
        async with _hx.AsyncClient(timeout=15) as c:
            r = await c.post("https://api.2captcha.com/createTask", json={
                "clientKey": api_key,
                "task": {"type": "ImageToTextTask", "body": b64}
            })
            data = r.json()
            if data.get("errorId"):
                return None
            task_id = data["taskId"]

        for _ in range(12):
            await asyncio.sleep(5)
            async with _hx.AsyncClient(timeout=15) as c:
                r = await c.post("https://api.2captcha.com/getTaskResult", json={
                    "clientKey": api_key,
                    "taskId": task_id,
                })
                data = r.json()
                if data.get("status") == "ready":
                    return data["solution"].get("text", "").strip()
        return None
    except Exception:
        return None


async def _extrair_site_key_recaptcha(page) -> str | None:
    """Extrai o site key do reCAPTCHA do conteúdo da página ou iframes."""
    html = await page.content()
    m = re.search(r'data-sitekey=["\']([A-Za-z0-9_-]{20,})["\']', html)
    if m:
        return m.group(1)
    # Tenta nos iframes do reCAPTCHA
    for fr in page.frames:
        url_fr = fr.url or ""
        k = re.search(r'[?&]k=([A-Za-z0-9_-]{20,})', url_fr)
        if k:
            return k.group(1)
    return None


def _parse_proxy(proxy_url: str) -> dict:
    """Converte URL de proxy (com credenciais embutidas) para dict do Playwright.
    Ex: http://user:pass@host:port → {"server": "http://host:port", "username": ..., "password": ...}
    """
    import urllib.parse
    p = urllib.parse.urlparse(proxy_url)
    proxy: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


def _env_sem_proxy() -> dict:
    """Retorna cópia do ambiente do processo sem variáveis de proxy SOCKS/HTTP.
    Passada ao Chromium via `env=` para que o processo filho não herde o proxy
    injetado pelo Railway.
    """
    _PROXY_KEYS = {
        'all_proxy', 'http_proxy', 'https_proxy',
        'socks_proxy', 'socks4_proxy', 'socks5_proxy',
        'no_proxy', 'proxy',
    }
    return {
        k: v for k, v in os.environ.items()
        if k.lower() not in _PROXY_KEYS
    }


_CHROMIUM_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--no-proxy-server",
    "--proxy-bypass-list=*",
]

_CHROMIUM_ARGS_SEM_PROXY_FLAG = [
    # Versão SEM --no-proxy-server — usada quando queremos passar proxy explícito
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]


# Origens GOV.BR / e-CAC para Playwright client_certificates (cada subdomínio precisa ser listado)
_ECAC_ORIGINS = [
    "https://cav.receita.fazenda.gov.br",
    "https://acesso.gov.br",
    "https://sso.acesso.gov.br",
    "https://login.acesso.gov.br",
    "https://contas.acesso.gov.br",
    "https://www.acesso.gov.br",
]
_ESOCIAL_ORIGINS = [
    "https://empregador.esocial.gov.br",
    "https://www.esocial.gov.br",
    "https://login.esocial.gov.br",
    "https://acesso.gov.br",
    "https://sso.acesso.gov.br",
    "https://login.acesso.gov.br",
    "https://contas.acesso.gov.br",
]


# ── DNS over HTTPS (DoH) — contorna resolução falha do Railway ───────────────

async def _resolver_doh(hostname: str) -> str | None:
    """Resolve hostname via Cloudflare DoH. Retorna primeiro IP A ou None."""
    import httpx as _hx
    try:
        async with _hx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A",
                headers={"Accept": "application/dns-json"},
            )
            for ans in r.json().get("Answer", []):
                if ans.get("type") == 1:
                    return ans["data"]
    except Exception:
        pass
    return None


# ── NSS Database — importa cert para que Chromium use no seletor GOV.BR ──────

async def _importar_cert_pfx_nss(pfx_bytes: bytes, senha: str) -> tuple[bool, str]:
    """
    Importa certificado PFX no banco NSS do Chromium (~/.pki/nssdb).
    Retorna (sucesso, nickname) para remoção posterior.
    Requer libnss3-tools (pk12util, certutil) — incluído na imagem Playwright.
    """
    import subprocess, tempfile as _tf
    nss_dir = os.path.expanduser("~/.pki/nssdb")
    os.makedirs(nss_dir, exist_ok=True)

    # Inicializa o banco NSS se vazio
    certutil_path = "/usr/bin/certutil"
    pk12util_path = "/usr/bin/pk12util"
    db_arg = f"sql:{nss_dir}"

    # Cria banco se não existir
    if not os.path.exists(os.path.join(nss_dir, "cert9.db")):
        subprocess.run(
            [certutil_path, "-N", "--empty-password", "-d", db_arg],
            capture_output=True, timeout=10,
        )

    # Salva PFX em arquivo temporário
    with _tf.NamedTemporaryFile(suffix=".pfx", delete=False) as f:
        f.write(pfx_bytes)
        pfx_tmp = f.name

    try:
        r = subprocess.run(
            [pk12util_path, "-i", pfx_tmp, "-d", db_arg, "-W", senha or ""],
            capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            return False, ""

        # Descobre o nickname importado
        r2 = subprocess.run(
            [certutil_path, "-L", "-d", db_arg],
            capture_output=True, timeout=10, text=True,
        )
        # Primeira linha que não é cabeçalho
        for line in r2.stdout.splitlines():
            if line.strip() and not line.startswith("Certificate") and not line.startswith("-"):
                nickname = line.split("  ")[0].strip()
                if nickname:
                    return True, nickname
        return True, ""
    finally:
        try:
            os.unlink(pfx_tmp)
        except Exception:
            pass


def _remover_cert_nss(nickname: str) -> None:
    """Remove certificado do banco NSS pelo nickname."""
    import subprocess
    if not nickname:
        return
    nss_dir = os.path.expanduser("~/.pki/nssdb")
    subprocess.run(
        ["/usr/bin/certutil", "-D", "-d", f"sql:{nss_dir}", "-n", nickname],
        capture_output=True, timeout=10,
    )

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


async def _run_playwright_multi(
    cert_pem: bytes,
    key_pem: bytes,
    origins: list[str],
    tarefa_fn,
    extra_chromium_args: list[str] | None = None,
    proxy_url: str | None = None,
) -> dict:
    """Lança Playwright com client certificate em múltiplas origens.
    proxy_url: http://user:pass@host:port — roteia pelo proxy quando fornecido.
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
        launch_kw: dict = {"headless": True, "args": args, "env": _env_sem_proxy()}
        if proxy_url:
            launch_kw["proxy"] = _parse_proxy(proxy_url)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**launch_kw)
            ctx_kwargs: dict = dict(
                client_certificates=[
                    {"origin": o, "certPath": cert_path, "keyPath": key_path}
                    for o in origins
                ],
                ignore_https_errors=True,
                user_agent=_UA,
            )
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


async def _pem_para_pfx_tmp(cert_pem: bytes, key_pem: bytes) -> str:
    """Converte PEM cert+key para arquivo PFX temporário sem senha.
    Necessário porque client_certificates do Playwright bypassa proxy;
    com NSS o Chromium carrega o cert sem bypass.
    """
    from cryptography import x509 as _cx509
    from cryptography.hazmat.primitives.serialization import load_pem_private_key, NoEncryption
    from cryptography.hazmat.primitives.serialization.pkcs12 import serialize_key_and_certificates
    cert = _cx509.load_pem_x509_certificate(cert_pem)
    key = load_pem_private_key(key_pem, password=None)
    pfx = serialize_key_and_certificates(
        name=b"cert", key=key, cert=cert, cas=None,
        encryption_algorithm=NoEncryption(),
    )
    with tempfile.NamedTemporaryFile(suffix=".pfx", delete=False) as f:
        f.write(pfx)
        return f.name


async def _importar_cert_pfx_nss_em(pfx_path: str, senha: str, tmp_home: str) -> tuple[bool, str]:
    """Importa PFX num banco NSS isolado em tmp_home/.pki/nssdb.
    Retorna (sucesso, nickname). Requer libnss3-tools na imagem.
    """
    import subprocess
    nss_dir = os.path.join(tmp_home, ".pki", "nssdb")
    os.makedirs(nss_dir, exist_ok=True)
    certutil = "/usr/bin/certutil"
    pk12util  = "/usr/bin/pk12util"
    db_arg = f"sql:{nss_dir}"

    # Inicializa banco vazio
    subprocess.run([certutil, "-N", "--empty-password", "-d", db_arg],
                   capture_output=True, timeout=10)

    # Importa PFX
    r = subprocess.run(
        [pk12util, "-i", pfx_path, "-d", db_arg, "-W", senha or ""],
        capture_output=True, timeout=20,
    )
    if r.returncode != 0:
        return False, ""

    # Descobre nickname importado
    r2 = subprocess.run([certutil, "-L", "-d", db_arg],
                        capture_output=True, timeout=10, text=True)
    for line in r2.stdout.splitlines():
        if line.strip() and not line.startswith(("Certificate", "-")):
            nick = line.split("  ")[0].strip()
            if nick:
                return True, nick
    return True, ""


async def _run_playwright_ecac_nss(
    pfx_path: str,
    pfx_senha: str,
    tarefa_fn,
    proxy_url: str | None = None,
) -> dict:
    """Lança Playwright com certificado importado no NSS isolado por sessão.
    proxy_url: quando fornecido, roteia a sessão pelo proxy residencial.
    Requer libnss3-tools (certutil + pk12util) na imagem.
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

        # Com proxy: usa _CHROMIUM_ARGS_SEM_PROXY_FLAG (sem --no-proxy-server)
        # Sem proxy: remove flags de proxy para não conflitar com NSS
        if proxy_url:
            args = list(_CHROMIUM_ARGS_SEM_PROXY_FLAG)
        else:
            args = [a for a in _CHROMIUM_ARGS if "proxy" not in a.lower()]

        launch_kw: dict = {"headless": True, "args": args, "env": env}
        if proxy_url:
            launch_kw["proxy"] = _parse_proxy(proxy_url)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**launch_kw)
            ctx_kwargs: dict = dict(
                ignore_https_errors=True,
                user_agent=_UA,
            )
            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            try:
                return await tarefa_fn(page, context)
            finally:
                await context.close()
                await browser.close()
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


async def _run_ecac_com_proxy(
    *,
    tarefa_fn,
    pfx_path: str | None = None,
    pfx_senha: str | None = None,
    cert_pem: bytes | None = None,
    key_pem: bytes | None = None,
    origins: list[str] | None = None,
    extra_chromium_args: list[str] | None = None,
    prefer_brazil: bool = True,
    max_tentativas: int = 3,
) -> dict:
    """Executa tarefa e-CAC/eSocial roteando pelo ProxyManager com retry automático.
    Em caso de falha de rede, marca o proxy como falho e tenta o próximo.
    Se ProxyManager não estiver configurado, executa sem proxy (comportamento anterior).
    """
    import time as _time

    manager = get_proxy_manager()

    for tentativa in range(max_tentativas):
        proxy_url: str | None = None
        if manager:
            # Reusa o proxy do manager mesmo se repetido entre tentativas — com pool
            # pequeno (às vezes 1 único proxy) cair para conexão direta (sem proxy)
            # garante falha de DNS no Railway para domínios gov.br (ERR_NAME_NOT_RESOLVED).
            proxy_url = await manager.get_proxy(prefer_brazil=prefer_brazil)

        t0 = _time.monotonic()
        tmp_pfx: str | None = None
        try:
            if pfx_path and pfx_senha is not None:
                # Já tem PFX — usa NSS diretamente
                resultado = await _run_playwright_ecac_nss(
                    pfx_path, pfx_senha, tarefa_fn, proxy_url=proxy_url,
                )
            else:
                # client_certificates do Playwright bypassa proxy (bug de design do Playwright)
                # → converte PEM→PFX e usa NSS para que o Chromium carregue o cert sem bypass
                tmp_pfx = await _pem_para_pfx_tmp(cert_pem, key_pem)
                resultado = await _run_playwright_ecac_nss(
                    tmp_pfx, "", tarefa_fn, proxy_url=proxy_url,
                )
            elapsed_ms = (_time.monotonic() - t0) * 1000
            if proxy_url and manager:
                manager.record_success(proxy_url, elapsed_ms)
            return resultado

        except Exception as e:
            if proxy_url and manager:
                manager.record_failure(proxy_url)
            err_str = str(e).lower()
            is_last = tentativa == max_tentativas - 1
            if is_last:
                raise
            # Retenta apenas em erros de rede/proxy; outros erros propagam imediatamente
            if not any(k in err_str for k in ("net::", "connection", "proxy", "timeout", "socks")):
                raise
        finally:
            if tmp_pfx:
                try: os.unlink(tmp_pfx)
                except Exception: pass

    # Não deve chegar aqui — o raise no is_last já propaga
    raise RuntimeError("Todas as tentativas de conexão falharam.")


async def _run_playwright_no_cert(tarefa_fn, proxy_url: str | None = None) -> dict:
    """Playwright sem certificado cliente — para portais públicos (CND Federal, TST, FGTS).
    Usa proxy residencial automaticamente via ProxyManager se disponível.
    """
    from playwright.async_api import async_playwright

    # Busca proxy do ProxyManager ou fallback estático
    if proxy_url is None:
        manager = get_proxy_manager()
        if manager:
            proxy_url = await manager.get_proxy(prefer_brazil=True)
        if not proxy_url:
            from ..config import settings as _cfg_pub
            proxy_url = _cfg_pub.PROXY_RESIDENCIAL_URL or None

    args = _CHROMIUM_ARGS_SEM_PROXY_FLAG if proxy_url else _CHROMIUM_ARGS
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=args,
            env=_env_sem_proxy(),
        )
        ctx_kwargs: dict = dict(ignore_https_errors=True, user_agent=_UA)
        if proxy_url:
            ctx_kwargs["proxy"] = {"server": proxy_url}
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        try:
            return await tarefa_fn(page)
        finally:
            await context.close()
            await browser.close()


async def _run_playwright(cert_pem: bytes, key_pem: bytes, origin: str, tarefa_fn) -> dict:
    from playwright.async_api import async_playwright

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as cf:
        cf.write(cert_pem); cert_path = cf.name
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as kf:
        kf.write(key_pem); key_path = kf.name

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=_CHROMIUM_ARGS,
                env=_env_sem_proxy(),
            )
            context = await browser.new_context(
                client_certificates=[{"origin": origin, "certPath": cert_path, "keyPath": key_path}],
                ignore_https_errors=True,
                user_agent=_UA,
            )
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


def _raise_http(e: Exception):
    msg = str(e)
    if "timeout" in msg.lower():
        raise HTTPException(504, "Tempo esgotado ao conectar ao portal. Tente novamente.")
    if "net::" in msg or "SSL" in msg:
        raise HTTPException(502, f"Erro de conexão: {msg}")
    raise HTTPException(502, f"Erro na automação: {msg}")


async def _upsert_receita(db, escritorio_id, cliente_id, competencia, valor, origem):
    r = await db.execute(select(ReceitaHistorica).where(
        ReceitaHistorica.cliente_id == cliente_id,
        ReceitaHistorica.escritorio_id == escritorio_id,
        ReceitaHistorica.competencia == competencia,
    ))
    ex = r.scalar_one_or_none()
    if ex:
        ex.valor_receita = valor
        ex.origem = origem
    else:
        db.add(ReceitaHistorica(
            escritorio_id=escritorio_id, cliente_id=cliente_id,
            competencia=competencia, valor_receita=valor, origem=origem,
        ))


async def _upsert_folha(db, escritorio_id, cliente_id, competencia, valor):
    r = await db.execute(select(FolhaMensal).where(
        FolhaMensal.cliente_id == cliente_id,
        FolhaMensal.escritorio_id == escritorio_id,
        FolhaMensal.competencia == competencia,
    ))
    ex = r.scalar_one_or_none()
    if ex:
        ex.valor_salarios = valor
        ex.valor_total = valor
        ex.origem = "esocial"
    else:
        db.add(FolhaMensal(
            escritorio_id=escritorio_id, cliente_id=cliente_id,
            competencia=competencia, valor_salarios=valor,
            valor_total=valor, origem="esocial",
        ))


# ─── Tarefa PGDAS-D ──────────────────────────────────────────────────────────

async def _tarefa_pgdas(page, context, cnpj: str, ano: int, usar_procuracao: bool = False) -> dict:
    """
    Navega no portal do Simples Nacional/e-CAC e extrai receita bruta por competência.
    Quando usar_procuracao=True: usa o cert do escritório via e-CAC + procuração.
    Quando usar_procuracao=False: usa cert próprio do cliente no Simples Nacional.
    """
    from datetime import date as _dt_date
    cnpj_limpo = re.sub(r"\D", "", cnpj)
    cnpj_fmt = f"{cnpj_limpo[:2]}.{cnpj_limpo[2:5]}.{cnpj_limpo[5:8]}/{cnpj_limpo[8:12]}-{cnpj_limpo[12:]}"
    receitas: list[dict] = []
    _hoje = _dt_date.today()
    mes_inicio = _hoje.month - 1 if _hoje.year == ano else 12
    if mes_inicio < 1:
        mes_inicio = 12

    # ── Fluxo via procuração (cert do escritório, e-CAC) ──────────────────────
    if usar_procuracao:
        # 1. Acessa e-CAC — o cert é apresentado automaticamente pelo Playwright
        await page.goto(
            "https://cav.receita.fazenda.gov.br/autenticacao/login",
            wait_until="domcontentloaded", timeout=_PGDAS_TIMEOUT,
        )
        await page.wait_for_timeout(3000)
        for sel in ["a:has-text('Certificado Digital')", "button:has-text('Certificado Digital')",
                    "input[value*='ertificado']", "#btnCertificado",
                    "a:has-text('certificado')", "button:has-text('certificado')"]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

        # 2. Aguarda e trata o seletor de certificado do GOV.BR (acesso.gov.br/selecionar-certificado)
        # O NSS garante que o cert aparece na lista; aqui clicamos nele.
        for _tentativa in range(15):  # até 45 segundos
            url_atual = page.url
            titulo_atual = await page.title()

            # Se chegou no seletor GOV.BR → seleciona o certificado
            if "acesso.gov.br" in url_atual or "selecionar-certificado" in url_atual:
                await page.wait_for_timeout(2000)
                # Tenta clicar no primeiro certificado disponível na lista
                for sel in [
                    "ul.certificate-list li:first-child button",
                    ".certificate-item:first-child button",
                    ".cert-item:first-child",
                    "li.certificate:first-child a",
                    "button.cert-select", "a.cert-select",
                    "input[type='radio']:first-of-type",
                    ".certlist li:first-child button",
                    "div[class*='certificate'] button",
                ]:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click()
                            await page.wait_for_timeout(1500)
                            break
                    except Exception:
                        pass
                # Confirma a seleção (botão Entrar / Selecionar / Confirmar)
                for sel in [
                    "button:has-text('Entrar')", "button:has-text('Selecionar')",
                    "button:has-text('Confirmar')", "button.btn-primary",
                    "input[type='submit']", "button[type='submit']",
                ]:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click()
                            await page.wait_for_load_state("domcontentloaded", timeout=30000)
                            await page.wait_for_timeout(3000)
                            break
                    except Exception:
                        pass
                break  # sai do loop de tentativas — continua o fluxo

            # Se já está no e-CAC autenticado → segue
            if "cav.receita.fazenda.gov.br/ecac" in url_atual and "autenticacao" not in url_atual:
                break

            # Se ficou na página de login → aguarda mais (pode estar processando)
            await page.wait_for_timeout(3000)

        # 3. Navega explicitamente para /ecac/ e verifica se autenticou
        await page.goto(
            "https://cav.receita.fazenda.gov.br/ecac/",
            wait_until="domcontentloaded", timeout=_PGDAS_TIMEOUT,
        )
        await page.wait_for_timeout(3000)
        url_apos_ecac = page.url
        if "autenticacao" in url_apos_ecac.lower() or "login" in url_apos_ecac.lower():
            titulo_pg = await page.title()
            return {
                "receitas": [],
                "aviso": (
                    f"Autenticação com certificado falhou no e-CAC. "
                    f"URL: {url_apos_ecac} | Título: {titulo_pg}. "
                    f"Verifique: (1) certificado .pfx válido e senha correta; "
                    f"(2) procuração eletrônica ativa no e-CAC para o escritório; "
                    f"(3) certificado não expirado. "
                    f"Se o GOV.BR mostrou tela de seleção de cert, pode ser que o "
                    f"libnss3-tools não está instalado na imagem Railway."
                ),
            }

        # 3. Clica em "Alterar perfil de acesso" para acessar como procurador do cliente
        for sel in [
            "a:has-text('Alterar perfil')", "button:has-text('Alterar perfil')",
            "#lnkAlterarPerfil", "a:has-text('Trocar perfil')",
            "a:has-text('Acessar como procurador')", "a[href*='alterarPerfil']",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                pass

        # 4. Seleciona o cliente por CNPJ na lista de procurados
        for sel in [
            f"a:has-text('{cnpj_fmt}')", f"a:has-text('{cnpj_limpo[:14]}')",
            f"a:has-text('{cnpj_limpo[:8]}')", f"td:has-text('{cnpj_limpo}')",
            f"tr:has-text('{cnpj_fmt}') a", f"tr:has-text('{cnpj_limpo[:8]}') a",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

        # 5. Navega para a página de PGDAS-D no e-CAC (URL exata que a usuária usa)
        await page.goto(
            "https://cav.receita.fazenda.gov.br/ecac/Aplicacao.aspx?id=10009&origem=menu",
            wait_until="domcontentloaded", timeout=_PGDAS_TIMEOUT,
        )
        await page.wait_for_timeout(4000)

        # 6. Se a página ainda mostra lista de clientes, seleciona o correto
        try:
            el = page.locator(
                f"a:has-text('{cnpj_fmt}'), a:has-text('{cnpj_limpo[:8]}'), "
                f"td:has-text('{cnpj_limpo}')"
            ).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_timeout(3000)
        except Exception:
            pass

        html = await page.content()
        # 5. Extrai o PA mais recente — a página do e-CAC mostra "PA MM/AAAA" como seções
        mes_ant_ref = mes_inicio
        ano_ref_loop = ano if _hoje.month > 1 else ano - 1
        receitas = _parse_pgdas_lxml(html, ano)

        # 6. Tenta clicar na declaração do PA mais recente para obter RBT12
        if not receitas:
            for mes in range(mes_inicio, 0, -1):
                comp_slash = f"{mes:02d}/{ano}"
                pa_label = f"PA {comp_slash}"
                try:
                    # Encontra a seção PA e clica no ícone de declaração
                    for sel in [
                        f"tr:near(:text('{pa_label}')) a[href*='declara'], "
                        f"tr:near(:text('{pa_label}')) img[title*='eclara']",
                        f"a:near(:text('{pa_label}'))",
                        f"a[title*='{comp_slash}']",
                    ]:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click()
                            await page.wait_for_load_state("domcontentloaded", timeout=15000)
                            await page.wait_for_timeout(2000)
                            html2 = await page.content()
                            month_recs = _parse_pgdas_lxml(html2, ano, mes_fixo=mes)
                            if month_recs:
                                receitas.extend(month_recs)
                            await page.go_back()
                            await page.wait_for_timeout(1500)
                            break
                except Exception:
                    pass
                if receitas:
                    break

        debug_url = page.url
        _hoje_av = _dt_date.today()
        mes_ant = _hoje_av.month - 1 if _hoje_av.month > 1 else 12
        ano_ant = _hoje_av.year if _hoje_av.month > 1 else _hoje_av.year - 1
        aviso = (
            f"PGDAS-D (e-CAC/procuração): {len(receitas)} competência(s) importada(s). "
            f"Último período buscado: {mes_ant:02d}/{ano_ant}."
            if receitas
            else (
                f"e-CAC acessado via procuração (URL: {debug_url}). "
                f"Nenhum dado de {mes_ant:02d}/{ano} encontrado. Verifique se há PGDAS-D transmitido "
                "para este período e se a procuração está ativa."
            )
        )
        return {"receitas": receitas, "aviso": aviso}

    # ── Fluxo direto com cert do próprio cliente (Simples Nacional) ───────────
    # 1. Autentica via e-CAC
    await page.goto(
        "https://cav.receita.fazenda.gov.br/autenticacao/login",
        wait_until="domcontentloaded", timeout=_PGDAS_TIMEOUT,
    )
    await page.wait_for_timeout(3000)
    for sel in ["a:has-text('Certificado Digital')", "button:has-text('Certificado Digital')",
                "input[value*='ertificado']", "#btnCertificado"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
                break
        except Exception:
            pass

    # 2. Tenta URL direta do e-CAC para PGDAS-D
    await page.goto(
        "https://cav.receita.fazenda.gov.br/ecac/Aplicacao.aspx?id=10009&origem=menu",
        wait_until="domcontentloaded", timeout=_PGDAS_TIMEOUT,
    )
    await page.wait_for_timeout(3000)
    html = await page.content()
    receitas = _parse_pgdas_lxml(html, ano)

    # 3. Navega para Simples Nacional se necessário
    if not receitas:
        await page.goto(
            "https://www8.receita.fazenda.gov.br/SimplesNacional/",
            wait_until="domcontentloaded", timeout=_PGDAS_TIMEOUT,
        )
        await page.wait_for_timeout(3000)
        try:
            el = page.locator(f"a:has-text('{cnpj_limpo[:8]}')").first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
        except Exception:
            pass

        await page.goto(
            "https://www8.receita.fazenda.gov.br/SimplesNacional/Aplicacoes/ATBHE/pgdas.app/emPGDAS/",
            wait_until="domcontentloaded", timeout=_PGDAS_TIMEOUT,
        )
        await page.wait_for_timeout(3000)
        html = await page.content()
        receitas = _parse_pgdas_lxml(html, ano)

    # 4. Itera pelos meses do mais recente para o mais antigo
    if not receitas:
        for mes in range(mes_inicio, 0, -1):
            comp_slash = f"{mes:02d}/{ano}"
            try:
                el = page.locator(
                    f"a:has-text('{comp_slash}'), td:has-text('{comp_slash}'), "
                    f"[title='{comp_slash}'], [data-competencia='{comp_slash}']"
                ).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(1500)
                    html = await page.content()
                    month_recs = _parse_pgdas_lxml(html, ano, mes_fixo=mes)
                    if month_recs:
                        receitas.extend(month_recs)
                        await page.go_back()
                        await page.wait_for_timeout(1000)
                        break
                    await page.go_back()
                    await page.wait_for_timeout(1500)
            except Exception:
                pass

    debug_url = page.url
    _hoje_av2 = _dt_date.today()
    mes_ant2 = _hoje_av2.month - 1 if _hoje_av2.month > 1 else 12
    ano_ant2 = _hoje_av2.year if _hoje_av2.month > 1 else _hoje_av2.year - 1
    aviso = (
        f"PGDAS-D: {len(receitas)} competência(s) importada(s). "
        f"Último período buscado: {mes_ant2:02d}/{ano_ant2}."
        if receitas
        else (
            f"Acesso ao PGDAS-D estabelecido (URL: {debug_url}). "
            f"Nenhum PGDAS de {mes_ant2:02d}/{ano} encontrado — verifique se há declaração transmitida "
            "para este período no portal do Simples Nacional."
        )
    )
    return {"receitas": receitas, "aviso": aviso}


# ─── Tarefa eSocial ──────────────────────────────────────────────────────────

async def _tarefa_esocial(page, context, cnpj: str, ano: int, usar_procuracao: bool = False) -> dict:
    """
    Navega no portal do eSocial e extrai totais de folha (Totalizadores → Empregador).
    Quando usar_procuracao=True: usa cert do escritório + troca perfil para o cliente.
    """
    from datetime import date as _dt_date
    cnpj_limpo = re.sub(r"\D", "", cnpj)
    cnpj_fmt = f"{cnpj_limpo[:2]}.{cnpj_limpo[2:5]}.{cnpj_limpo[5:8]}/{cnpj_limpo[8:12]}-{cnpj_limpo[12:]}"
    folhas: list[dict] = []
    _hoje = _dt_date.today()
    mes_ant = _hoje.month - 1 if _hoje.month > 1 else 12
    ano_ant = _hoje.year if _hoje.month > 1 else _hoje.year - 1

    # ── Login ─────────────────────────────────────────────────────────────────
    # empregador.esocial.gov.br: DNS do Railway não resolve, mas o buscar_esocial
    # passa --host-resolver-rules com IP resolvido via DoH (Cloudflare).
    # Se o IP não foi resolvido, cai para www como fallback.
    await page.goto(
        "https://empregador.esocial.gov.br/",
        wait_until="domcontentloaded", timeout=_ESOCIAL_TIMEOUT,
    )
    await page.wait_for_timeout(4000)

    # Se empregador falhou (DNS), tenta www como fallback
    if "empregador" not in page.url and "esocial" in page.url:
        pass  # www já está carregado
    elif "error" in (await page.title()).lower() or len(await page.content()) < 500:
        await page.goto("https://www.esocial.gov.br/portal/", wait_until="domcontentloaded", timeout=_ESOCIAL_TIMEOUT)
        await page.wait_for_timeout(3000)

    # Tenta navegar para a área do empregador se estiver na www
    for sel in ["a:has-text('Empregador')", "a[href*='empregador']", "a[href*='Empregador']",
                "a:has-text('Acessar')", "button:has-text('Acessar')"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)
                break
        except Exception:
            pass
    # Clica em Certificado Digital para autenticar
    for sel in ["a:has-text('Certificado Digital')", "button:has-text('Certificado')",
                "input[value*='ertificado']", ".certificado",
                "a:has-text('certificado')", "button:has-text('certificado digital')"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                break
        except Exception:
            pass

    # ── Troca perfil para o cliente (procuração) ──────────────────────────────
    if usar_procuracao:
        for sel in [
            "a:has-text('Trocar Perfil')", "button:has-text('Trocar Perfil')",
            "a:has-text('Trocar Módulo')", "a:has-text('Trocar perfil/módulo')",
            "#lnkTrocarPerfil", ".trocar-perfil",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                pass

        # Seleciona o empregador (cliente) na lista
        for sel in [
            f"a:has-text('{cnpj_fmt}')", f"a:has-text('{cnpj_limpo[:8]}')",
            f"td:has-text('{cnpj_limpo}')", f"tr:has-text('{cnpj_fmt}')",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

    # ── Navega para Folha de Pagamento → Totalizadores → Empregador ──────────
    # Tenta navegação via menu
    for step_seq in [
        # Sequência real do portal eSocial (baseada na captura de tela da usuária)
        ["a:has-text('Folha de Pagamento')", "a:has-text('Totalizadores')", "a:has-text('Empregador')"],
        ["a:has-text('Folha de Pagamento')", "a:has-text('Gestão de Folha')"],
        ["a:has-text('Totalizadores')", "a:has-text('Empregador')"],
        ["a:has-text('Totalizadores')"],
    ]:
        try:
            for step_sel in step_seq:
                el = page.locator(step_sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(2000)
            html = await page.content()
            folhas = _parse_esocial_lxml(html, ano)
            if folhas:
                break
        except Exception:
            pass

    # ── Seleciona o mês anterior no calendário ────────────────────────────────
    if not folhas:
        nomes_mes = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
        nome_mes = nomes_mes[mes_ant - 1]
        for sel in [
            f"a:has-text('{nome_mes}')", f"button:has-text('{nome_mes}')",
            f"td:has-text('{nome_mes}')", f"[data-mes='{mes_ant}']",
            f"a:has-text('{mes_ant:02d}/{ano_ant}')", f"a:has-text('{mes_ant:02d}/{ano}')",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    folhas = _parse_esocial_lxml(html, ano)
                    if folhas:
                        break
            except Exception:
                pass

    # ── Tenta Totalizadores → Contribuição Previdenciária via submenu ─────────
    if not folhas:
        for sel in ["a:has-text('Contribuição Previdenciária')", "a:has-text('Contribuição')",
                    "a:has-text('FGTS')", "a:has-text('Imposto de Renda')"]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    folhas = _parse_esocial_lxml(html, ano)
                    if folhas:
                        break
            except Exception:
                pass

    # ── Fallback: tenta API REST do eSocial ───────────────────────────────────
    if not folhas:
        try:
            folhas = await _tentar_api_esocial(page, context, cnpj, ano)
        except Exception:
            pass

    modo = "procuração" if usar_procuracao else "certificado do cliente"
    aviso = (
        f"eSocial ({modo}): {len(folhas)} competência(s) de folha importada(s). "
        f"Último mês buscado: {mes_ant:02d}/{ano_ant}."
        if folhas
        else (
            f"eSocial ({modo}) acessado. Nenhuma folha de {mes_ant:02d}/{ano_ant} encontrada. "
            "Os totalizadores (S-5011) dependem do fechamento mensal (S-1299). "
            "Se ainda não foi fechado, lance manualmente no campo de folha."
        )
    )
    return {"folhas": folhas, "aviso": aviso}


async def _tentar_api_esocial(page, context, cnpj: str, ano: int) -> list:
    """Tenta buscar totalizadores via API REST do eSocial (se autenticado via cookie).
    Itera do mês anterior ao atual para trás (busca o mais recente primeiro).
    """
    folhas = []
    cnpj_limpo = re.sub(r"\D", "", cnpj)
    from datetime import date as _dt_date
    _hoje = _dt_date.today()
    mes_inicio = _hoje.month - 1 if _hoje.year == ano else 12
    if mes_inicio < 1:
        mes_inicio = 12
    for mes in range(mes_inicio, 0, -1):
        comp = f"{ano}{mes:02d}"
        comp_key = f"{ano}-{mes:02d}"
        api_urls = [
            f"https://apies.esocial.gov.br/api/consulta/totalizadores/folha?competencia={comp}&cnpj={cnpj_limpo}",
            f"https://empregador.esocial.gov.br/api/totalizadores?competencia={comp}",
        ]
        for api_url in api_urls:
            try:
                resp = await page.evaluate(f"""
                    fetch('{api_url}', {{credentials:'include'}})
                    .then(r => r.ok ? r.json() : null)
                    .catch(() => null)
                """)
                if resp and isinstance(resp, dict):
                    valor = (
                        resp.get("totalRemuneracao") or
                        resp.get("valorTotal") or
                        resp.get("total") or 0
                    )
                    if valor:
                        folhas.append({"competencia": comp_key, "valor_total_folha": float(valor)})
                        break
            except Exception:
                pass
    return folhas


# ─── Tarefa e-CAC faturamento LP/LR ─────────────────────────────────────────

async def _tarefa_efd_contribuicoes(page, context, cnpj: str, ano: int) -> dict:
    """
    Extrai receita bruta via EFD Contribuições (SPED) pelo e-CAC.

    Fluxo:
      e-CAC → Declarações e Demonstrativos → EFD-Contribuições
      → seleciona período → extrai base PIS/COFINS (= receita bruta)
    """
    receitas: list[dict] = []

    # ── 1. Autentica no e-CAC ────────────────────────────────────────────────
    await page.goto("https://cav.receita.fazenda.gov.br/autenticacao/login",
                    wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(3000)

    for sel in ["a:has-text('Certificado Digital')", "button:has-text('Certificado Digital')",
                "input[value*='ertificado']", "#btnCertificado"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=20000)
                await page.wait_for_timeout(2000)
                break
        except Exception:
            pass

    # ── 2. Tenta acessar EFD Contribuições via SPED portal ──────────────────
    efd_urls = [
        "https://sped.rfb.gov.br/contribuicoes/",
        "https://sped.rfb.gov.br/",
        "https://cav.receita.fazenda.gov.br/eCAC/ConsultarDeclaracoes?tipo=EFD-Contribuicoes",
    ]
    for url in efd_urls:
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)
            html = await page.content()
            recs = _parse_efd_contribuicoes_html(html, ano)
            if recs:
                receitas.extend(recs)
                break
        except Exception:
            pass

    # ── 3. Navega pelos menus do e-CAC buscando EFD ──────────────────────────
    if not receitas:
        await page.goto("https://cav.receita.fazenda.gov.br/eCAC/", wait_until="networkidle",
                        timeout=30000)
        await page.wait_for_timeout(2000)
        for nav_sel in [
            "a:has-text('Declarações')", "a:has-text('SPED')", "a:has-text('EFD')",
            "a:has-text('Contribuições')", "a:has-text('Demonstrativos')",
        ]:
            try:
                el = page.locator(nav_sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await page.wait_for_timeout(1500)
                    html = await page.content()
                    recs = _parse_efd_contribuicoes_html(html, ano)
                    if recs:
                        receitas.extend(recs)
                        break
            except Exception:
                pass

    aviso = (
        f"EFD-Contribuições: {len(receitas)} competência(s) extraída(s) de {ano}."
        if receitas
        else (
            f"Acesso ao portal EFD-Contribuições estabelecido (URL: {page.url}). "
            f"Nenhum dado de {ano} encontrado. Verifique se a empresa transmitiu "
            "a EFD-Contribuições para o período via SPED."
        )
    )
    return {"receitas": receitas, "aviso": aviso}


def _parse_efd_contribuicoes_html(html: str, ano: int) -> list:
    """Extrai receita bruta do HTML da página EFD-Contribuições do e-CAC."""
    receitas = []
    try:
        from lxml import html as lx
        tree = lx.fromstring(html)
        for table in tree.iter("table"):
            headers = [" ".join(th.text_content().split()).upper() for th in table.iter("th")]
            receita_idx = next((i for i, h in enumerate(headers)
                                if "RECEITA BRUTA" in h or "BASE" in h or "PIS" in h), None)
            comp_idx = next((i for i, h in enumerate(headers)
                             if "COMPETÊNCIA" in h or "PERÍODO" in h or "MÊS" in h), None)
            if receita_idx is None:
                continue
            for tr in table.iter("tr"):
                cells = [" ".join(td.text_content().split()) for td in tr.iter("td")]
                if len(cells) <= receita_idx:
                    continue
                comp_text = cells[comp_idx] if comp_idx is not None and comp_idx < len(cells) else ""
                m = re.search(r'(\d{2})/(\d{4})', comp_text)
                if not m:
                    continue
                mes, a = int(m.group(1)), int(m.group(2))
                if a != ano:
                    continue
                val_text = re.sub(r'[^\d,.]', '', cells[receita_idx])
                try:
                    val = float(val_text.replace('.', '').replace(',', '.'))
                    if val > 0:
                        receitas.append({"competencia": f"{ano}-{mes:02d}", "valor_receita": val})
                except ValueError:
                    pass
    except Exception:
        pass

    # Fallback regex
    if not receitas:
        for m in re.finditer(r'(\d{2})/(\d{4})[^<]*?R\$\s*([\d.,]+)', html):
            mes, a = int(m.group(1)), int(m.group(2))
            if a != ano:
                continue
            try:
                val = float(m.group(3).replace('.', '').replace(',', '.'))
                if val > 0:
                    receitas.append({"competencia": f"{ano}-{mes:02d}", "valor_receita": val})
            except ValueError:
                pass
    return receitas


async def _tarefa_ecac_faturamento(page, context, cnpj: str, ano: int) -> dict:
    """
    Extrai faturamento de empresas LP/LR via DCTFWeb → MIT (Módulo de Informações Tributárias).

    Fluxo:
      e-CAC (cav.receita.fazenda.gov.br) → DCTFWeb → aba MIT → seleciona período → extrai receita bruta
    """
    cnpj_limpo = re.sub(r"\D", "", cnpj)
    receitas: list[dict] = []

    # ── 1. Entra no e-CAC com certificado ────────────────────────────────────
    # O Playwright já tem o cert configurado para cav.receita.fazenda.gov.br.
    # A URL pública de entrada é o login do e-CAC; com cert o browser autentica automaticamente.
    await page.goto(
        "https://cav.receita.fazenda.gov.br/autenticacao/login",
        wait_until="domcontentloaded",
        timeout=_PGDAS_TIMEOUT,
    )
    await page.wait_for_timeout(3000)

    # Se aparecer seleção de tipo de acesso, escolhe "Certificado Digital"
    for sel in [
        "a:has-text('Certificado Digital')",
        "button:has-text('Certificado Digital')",
        "input[value*='ertificado']",
        "#btnCertificado",
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                break
        except Exception:
            pass

    # ── 2. Navega ao DCTFWeb ──────────────────────────────────────────────────
    # Tenta URL direta primeiro; se falhar, navega pelo menu do e-CAC.
    dctfweb_acessado = False
    for dctf_url in [
        "https://dctfweb.receita.fazenda.gov.br/dctfweb/",
        "https://dctfweb.receita.fazenda.gov.br/",
    ]:
        try:
            await page.goto(dctf_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            if "dctfweb" in page.url.lower():
                dctfweb_acessado = True
                break
        except Exception:
            pass

    if not dctfweb_acessado:
        # Tenta via menu do e-CAC
        for sel in [
            "a:has-text('DCTFWeb')",
            "a:has-text('DCTF Web')",
            "a[href*='dctfweb']",
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(3000)
                    dctfweb_acessado = True
                    break
            except Exception:
                pass

    # ── 3. Acessa aba MIT (Módulo de Informações Tributárias) ────────────────
    for sel in [
        "a:has-text('MIT')",
        "a:has-text('Módulo de Informações')",
        "a:has-text('Informações Tributárias')",
        "[aria-label*='MIT']",
        "button:has-text('MIT')",
        "li:has-text('MIT') a",
        "tab:has-text('MIT')",
        ".nav-item:has-text('MIT') a",
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
                await page.wait_for_timeout(3000)
                break
        except Exception:
            pass

    # ── 4. Seleciona o ano no MIT ─────────────────────────────────────────────
    try:
        # Tenta selecionar ano em dropdown
        sel_ano = page.locator(f"select option[value='{ano}']").first
        if await sel_ano.count() > 0:
            parent = page.locator(f"select:has(option[value='{ano}'])").first
            await parent.select_option(str(ano))
            await page.wait_for_timeout(2000)
        else:
            # Tenta campo de texto/input de ano
            inp = page.locator("input[name*='ano'], input[id*='ano'], input[placeholder*='ano']").first
            if await inp.count() > 0:
                await inp.fill(str(ano))
                await inp.press("Enter")
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
    except Exception:
        pass

    # ── 5. Clica em "Consultar" / "Pesquisar" se houver botão ────────────────
    for sel in [
        "button:has-text('Consultar')", "button:has-text('Pesquisar')",
        "input[type='submit'][value*='Consult']",
        "a:has-text('Consultar')",
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)
                break
        except Exception:
            pass

    # ── 6. Extrai receita bruta da tabela resultante ──────────────────────────
    html = await page.content()
    receitas = _parse_mit_lxml(html, ano)

    # ── 7. Fallback: itera pelos meses manualmente ───────────────────────────
    if not receitas:
        for mes in range(1, 13):
            comp_slash = f"{mes:02d}/{ano}"
            try:
                el = page.locator(
                    f"a:has-text('{comp_slash}'), td:has-text('{comp_slash}'), "
                    f"option:has-text('{comp_slash}')"
                ).first
                if await el.count() > 0:
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    if tag == "option":
                        parent = page.locator(f"select:has(option:has-text('{comp_slash}'))").first
                        await parent.select_option(label=comp_slash)
                    else:
                        await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(1500)
                    html_m = await page.content()
                    recs = _parse_mit_lxml(html_m, ano, mes_fixo=mes)
                    for r in recs:
                        if not any(x["competencia"] == r["competencia"] for x in receitas):
                            receitas.append(r)
                    await page.go_back()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

    aviso = (
        f"MIT/DCTFWeb: {len(receitas)} competência(s) de {ano} extraída(s)."
        if receitas
        else (
            "Acesso ao DCTFWeb estabelecido. MIT localizado mas sem dados de receita bruta "
            f"de {ano} encontrados na página atual. Verifique se há DCTFWs transmitidas para "
            "o período — se a empresa ainda não transmitiu, não há dados disponíveis."
        )
    )
    return {"receitas": receitas, "aviso": aviso}


# ─── Consulta automática de certidões ────────────────────────────────────────

async def _consultar_cnd_federal(cnpj: str) -> dict:
    """CND Federal — Receita Federal + PGFN.
    Tenta httpx direto (GET público) antes do Playwright.
    O portal RF bloqueia automação em ambientes cloud; httpx funciona para CNPJ simples.
    """
    import httpx as _httpx

    cnpj_digits = re.sub(r'\D', '', cnpj)

    # Resolve proxy para httpx e Playwright
    _proxy_rf: str | None = None
    _pm_rf = get_proxy_manager()
    if _pm_rf:
        _proxy_rf = await _pm_rf.get_proxy(prefer_brazil=True)
    if not _proxy_rf:
        from ..config import settings as _cfg_rf0
        _proxy_rf = _cfg_rf0.PROXY_RESIDENCIAL_URL or None

    # 1. Tentativa httpx — a RF tem endpoint de emissão via GET com CNPJ
    _RF_URLS = [
        f"https://solucoes.receita.fazenda.gov.br/servicos/certidaointernet/pj/emitir?NrCnpj={cnpj_digits}",
        f"https://solucoes.receita.fazenda.gov.br/servicos/certidaointernet/pj/Emitir?NrCnpj={cnpj_digits}",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
    }
    for url in _RF_URLS:
        try:
            client_kwargs: dict = dict(verify=False, timeout=30, follow_redirects=True, headers=headers)
            if _proxy_rf:
                client_kwargs["proxies"] = {"https://": _proxy_rf, "http://": _proxy_rf}
            async with _httpx.AsyncClient(**client_kwargs) as c:
                r = await c.get(url)
                if r.status_code == 200 and len(r.text) > 500 and "502" not in r.text[:200]:
                    result = _parse_certidao_html(r.text, "cnd_federal", page_url=str(r.url))
                    if result["status"] in ("regular", "irregular"):
                        return result
                    if result["status"] == "em_analise":
                        title_m = re.search(r'<title[^>]*>([^<]+)</title>', r.text, re.I)
                        title = title_m.group(1).strip() if title_m else ""
                        if "certid" in title.lower() or "receita" in title.lower() or "pgfn" in title.lower():
                            return result
        except Exception:
            pass

    # 2. Playwright como fallback
    if not await _playwright_ok():
        return {
            "status": "em_analise",
            "observacao": "Portal da Receita Federal bloqueado para acesso automatizado. Acesse manualmente: https://solucoes.receita.fazenda.gov.br/servicos/certidaointernet/pj/emitir"
        }

    _URL_RF = "https://solucoes.receita.fazenda.gov.br/servicos/certidaointernet/pj/emitir"

    async def _tarefa(page):
        await page.goto(_URL_RF, wait_until="domcontentloaded", timeout=40_000)
        await page.wait_for_timeout(2000)
        for sel in ['input[name="NrCnpj"]', 'input[name="cnpj"]', 'input[id*="cnpj" i]']:
            try:
                await page.fill(sel, cnpj_digits, timeout=3_000)
                break
            except Exception:
                pass
        for sel in ['input[type="image"]', 'button[type="submit"]', 'input[type="submit"]', 'a:has-text("Emitir")']:
            try:
                await page.click(sel, timeout=3_000)
                break
            except Exception:
                pass
        await page.wait_for_load_state("networkidle", timeout=40_000)
        result = _parse_certidao_html(await page.content(), "cnd_federal", page_url=page.url)
        if result["status"] == "regular":
            result = await _capturar_pdf_certidao(page, result)
        return result

    # 2. Playwright — usa _proxy_rf já resolvido acima
    try:
        # _run_playwright_no_cert já aplica proxy automaticamente quando disponível
        return await _run_playwright_no_cert(_tarefa, proxy_url=_proxy_rf)
    except Exception as e:
        return {
            "status": "em_analise",
            "observacao": f"Portal RF inacessível: {str(e)[:100]}. Acesse: {_URL_RF}"
        }


async def _consultar_cnd_fgts(cnpj: str, uf: str = "") -> dict:
    """CRF FGTS — Caixa Econômica Federal.
    URL: https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf
    Preenche CNPJ + UF do empregador e extrai o resultado.
    """
    cnpj_digits = re.sub(r'\D', '', cnpj)
    cnpj_fmt = f"{cnpj_digits[:2]}.{cnpj_digits[2:5]}.{cnpj_digits[5:8]}/{cnpj_digits[8:12]}-{cnpj_digits[12:]}"
    url_fgts = "https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf"
    url_manual = "https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf"

    if not await _playwright_ok():
        return {
            "status": "em_analise",
            "observacao": f"Playwright não disponível. Acesse manualmente: {url_manual}"
        }

    async def _tarefa_fgts(page):
        # Azion CDN usa JS challenge — precisa de networkidle + tempo extra para resolver
        await page.goto(url_fgts, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(5000)

        html = await page.content()
        if "Azion" in html or len(html) < 500:
            # Aguarda o JS challenge do Azion completar (pode levar até 10s)
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            await page.wait_for_timeout(5000)
            html = await page.content()

        if "Azion" in html or len(html) < 500:
            return {
                "status": "em_analise",
                "observacao": f"Portal CRF FGTS bloqueou acesso (CDN Azion). Acesse manualmente: {url_manual}"
            }

        # Preenche CNPJ (pode estar com ou sem máscara)
        for sel in [
            'input[id*="cnpj" i]', 'input[name*="cnpj" i]',
            'input[id*="empregador" i]', 'input[type="text"]:first-of-type',
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.fill(cnpj_digits)
                    break
            except Exception:
                pass

        # Seleciona UF se houver dropdown
        if uf:
            for sel in [
                'select[id*="uf" i]', 'select[name*="uf" i]',
                'select[id*="estado" i]', 'select[name*="estado" i]',
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.select_option(uf.upper())
                        break
                except Exception:
                    pass

        # Clica em Consultar
        for sel in [
            'input[type="submit"]', 'button[type="submit"]',
            'button:has-text("Consultar")', 'input[value*="Consultar" i]',
            'a:has-text("Consultar")',
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                pass

        html2 = await page.content()
        result = _parse_certidao_html(html2, "cnd_fgts", page_url=page.url)
        if result["status"] == "em_analise" and not result.get("observacao"):
            result["observacao"] = f"Acesse manualmente: {url_manual}"
        return result

    try:
        import time as _t_fgts
        manager = get_proxy_manager()
        proxy_url: str | None = None
        if manager:
            proxy_url = await manager.get_proxy(prefer_brazil=True)
        # Fallback: PROXY_RESIDENCIAL_URL estático se ProxyManager não carregou
        if not proxy_url:
            from ..config import settings as _cfg_fgts
            proxy_url = _cfg_fgts.PROXY_RESIDENCIAL_URL or None

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

            elapsed_fgts = (_t_fgts.monotonic() - t0_fgts) * 1000
            if proxy_url and manager:
                manager.record_success(proxy_url, elapsed_fgts)
            return resultado_fgts

        except Exception:
            if proxy_url and manager:
                manager.record_failure(proxy_url)
            raise

    except Exception as e:
        return {
            "status": "em_analise",
            "observacao": f"CRF FGTS: {str(e)[:150]}. Acesse: {url_manual}"
        }


async def _consultar_cndt_tst(cnpj: str) -> dict:
    """CNDT — TST Nacional. Usa Playwright (portal JS-rendered)."""
    if not await _playwright_ok():
        return {"status": "em_analise", "observacao": "Playwright não disponível. Acesse: https://www.tst.jus.br/certidao"}

    cnpj_digits = re.sub(r'\D', '', cnpj)

    async def _tarefa(page):
        await page.goto("https://www.tst.jus.br/certidao", wait_until="networkidle", timeout=40_000)
        for sel in ['input[name="cpf_cnpj"]', 'input[name="nrCpfCnpj"]', 'input[id*="cnpj" i]', 'input[type="text"]']:
            try:
                await page.fill(sel, cnpj_digits, timeout=3_000)
                break
            except Exception:
                pass
        for sel in ['button[type="submit"]', 'input[type="submit"]', 'input[type="image"]',
                    'a:has-text("Consultar")', 'button:has-text("Emitir")', 'button:has-text("Pesquisar")']:
            try:
                await page.click(sel, timeout=3_000)
                break
            except Exception:
                pass
        await page.wait_for_load_state("networkidle", timeout=40_000)
        result = _parse_certidao_html(await page.content(), "cndt_tst", page_url=page.url)

        if result["status"] == "regular":
            result = await _capturar_pdf_certidao(page, result)

        return result

    try:
        return await _run_playwright_no_cert(_tarefa)
    except Exception as e:
        return {"status": "em_analise", "observacao": f"Erro na consulta: {e}. Acesse: https://www.tst.jus.br/certidao"}


async def _consultar_cndt_trt(cnpj: str, uf: str) -> dict:
    """CNDT TRT Regional — URL varia por TRT/UF. Usa Playwright."""
    _TRT_URLS = {
        "RJ": "https://certidao.trt1.jus.br/certidao/",
        "SP": "https://www.trt2.jus.br/certidao/",
        "MG": "https://certidao.trt3.jus.br/certidao/",
        "RS": "https://www.trt4.jus.br/certidao/",
        "BA": "https://certidao.trt5.jus.br/certidao/",
        "PE": "https://certidao.trt6.jus.br/certidao/",
        "CE": "https://certidao.trt7.jus.br/certidao/",
        "PA": "https://certidao.trt8.jus.br/certidao/",
        "AP": "https://certidao.trt8.jus.br/certidao/",
        "PR": "https://certidao.trt9.jus.br/certidao/",
        "DF": "https://certidao.trt10.jus.br/certidao/",
        "TO": "https://certidao.trt10.jus.br/certidao/",
        "AM": "https://www.tst.jus.br/certidao",
        "RR": "https://www.tst.jus.br/certidao",
        "SC": "https://certidao.trt12.jus.br/certidao/",
        "PB": "https://certidao.trt13.jus.br/certidao/",
        "RO": "https://certidao.trt14.jus.br/certidao/",
        "AC": "https://certidao.trt14.jus.br/certidao/",
        "MA": "https://certidao.trt16.jus.br/certidao/",
        "ES": "https://certidao.trt17.jus.br/certidao/",
        "GO": "https://certidao.trt18.jus.br/certidao/",
        "AL": "https://certidao.trt19.jus.br/certidao/",
        "SE": "https://certidao.trt20.jus.br/certidao/",
        "RN": "https://certidao.trt21.jus.br/certidao/",
        "PI": "https://certidao.trt22.jus.br/certidao/",
        "MT": "https://certidao.trt23.jus.br/certidao/",
        "MS": "https://certidao.trt24.jus.br/certidao/",
    }
    trt_url = _TRT_URLS.get(uf.upper(), "https://www.tst.jus.br/certidao")

    if not await _playwright_ok():
        return {"status": "em_analise", "observacao": f"Playwright não disponível. Acesse: {trt_url}"}

    cnpj_digits = re.sub(r'\D', '', cnpj)

    async def _tarefa(page):
        await page.goto(trt_url, wait_until="networkidle", timeout=40_000)
        for sel in ['input[name="nrCpfCnpj"]', 'input[name="cpf_cnpj"]', 'input[name="cnpj"]', 'input[id*="cnpj" i]', 'input[type="text"]']:
            try:
                await page.fill(sel, cnpj_digits, timeout=3_000)
                break
            except Exception:
                pass
        for sel in ['button[type="submit"]', 'input[type="submit"]', 'input[type="image"]', 'a:has-text("Consultar")']:
            try:
                await page.click(sel, timeout=3_000)
                break
            except Exception:
                pass
        await page.wait_for_load_state("networkidle", timeout=40_000)
        result = _parse_certidao_html(await page.content(), "cndt_trt", page_url=page.url)
        result["observacao"] = f"TRT consultado: {uf} → {trt_url}"
        if result["status"] == "regular":
            result = await _capturar_pdf_certidao(page, result)
        return result

    try:
        return await _run_playwright_no_cert(_tarefa)
    except Exception as e:
        return {"status": "em_analise", "observacao": f"Erro na consulta TRT{uf}: {e}. Acesse: {trt_url}"}


async def _consultar_cnd_estadual(cnpj: str, uf: str, tipo: str) -> dict:
    """CND Estadual — varia por UF/SEFAZ. Usa Playwright para portais JS-rendered."""
    _SEFAZ_CNDS = {
        "AM": "https://www.sefaz.am.gov.br/portal/certidao-negativa",
        "SP": "https://www10.fazenda.sp.gov.br/CertidaoNegativaDeb/Pages/EmissaoCertidao.aspx",
        "RJ": "https://www4.fazenda.rj.gov.br/consultaDivida/pages/certidaoNegativa.jsf",
        "MG": "https://www.fazenda.mg.gov.br/empresas/impostos_estaduais/certidao/",
        "RS": "https://www.sefaz.rs.gov.br/SAT/CertidaoPJ.aspx",
        "PE": "https://www.sefaz.pe.gov.br/sefa/servlet/consulta",
        "CE": "https://cagec.sefaz.ce.gov.br/",
        "BA": "https://www.sefaz.ba.gov.br/certidao/",
    }
    url = _SEFAZ_CNDS.get(uf.upper(), "")
    if not url:
        sefaz = f"www.sefaz.{uf.lower()}.gov.br"
        return {
            "status": "em_analise",
            "observacao": f"Portal SEFAZ-{uf} ainda não integrado. Consulte manualmente em {sefaz}",
        }

    if not await _playwright_ok():
        return {"status": "em_analise", "observacao": f"Playwright não disponível. Acesse: {url}"}

    cnpj_digits = re.sub(r'\D', '', cnpj)

    async def _tarefa(page):
        await page.goto(url, wait_until="networkidle", timeout=40_000)
        for sel in ['input[name*="cnpj" i]', 'input[id*="cnpj" i]', 'input[type="text"]']:
            try:
                await page.fill(sel, cnpj_digits, timeout=3_000)
                break
            except Exception:
                pass
        for sel in ['button[type="submit"]', 'input[type="submit"]', 'input[type="image"]']:
            try:
                await page.click(sel, timeout=3_000)
                break
            except Exception:
                pass
        await page.wait_for_load_state("networkidle", timeout=40_000)
        result = _parse_certidao_html(await page.content(), tipo, page_url=page.url)
        if result["status"] == "regular":
            result = await _capturar_pdf_certidao(page, result)
        return result

    try:
        return await _run_playwright_no_cert(_tarefa)
    except Exception as e:
        return {"status": "em_analise", "observacao": f"Erro na consulta SEFAZ-{uf}: {e}. Acesse: {url}"}


async def _consultar_cnd_municipal(cnpj: str, municipio: str, uf: str) -> dict:
    """CND Municipal — varia por município."""
    mun_key = municipio.lower().strip()
    cnpj_digits = re.sub(r'\D', '', cnpj)

    # ── Manaus / SEMEF ────────────────────────────────────────────────────────
    if "manaus" in mun_key or uf.upper() == "AM":
        # Portal: semefatende.manaus.am.gov.br/servicoJanela.php?servico=1412
        # O STM exige CAPTCHA — tenta via Playwright, mas se CAPTCHA bloquear,
        # retorna link direto ao portal.
        url_manual = "https://semefatende.manaus.am.gov.br/servicoJanela.php?servico=1412"
        stm_url = "https://stm.manaus.am.gov.br/stm/servlet/hwvdocumentos_v3"

        if not await _playwright_ok():
            return {"status": "em_analise", "observacao": f"Acesse manualmente: {url_manual}"}

        async def _tarefa_manaus(page):
            # Tenta acesso direto ao servlet STM (pode funcionar sem CAPTCHA via POST)
            try:
                # wait_until="commit" dispara ao primeiro byte — portal SEMEF é lento,
                # não esperar networkidle que pode demorar 90s+. Espera fixa após commit.
                await page.goto(url_manual, wait_until="commit", timeout=90_000)
                await page.wait_for_timeout(8_000)  # aguarda DOM ficar interativo
                await page.wait_for_timeout(2000)
                # Seleciona radio "CNPJ" se existir
                for sel in ['input[value="CNPJ"]', 'input[type="radio"][value*="cnpj" i]',
                            'label:has-text("CNPJ") input']:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click()
                            await page.wait_for_timeout(500)
                            break
                    except Exception:
                        pass
                # Preenche o número do CNPJ
                for sel in ['input[id*="numero" i]', 'input[name*="numero" i]',
                            'input[id*="cnpj" i]', 'input[type="text"]']:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.fill(cnpj_digits)
                            break
                    except Exception:
                        pass
                # Aguarda carregamento dinâmico do CAPTCHA
                await page.wait_for_timeout(2500)
                html_cap = await page.content()
                if re.search(r'captcha|recaptcha|Recarregar|código de segurança|imgseg|gera_captcha|codigo_seguranca', html_cap, re.I):
                    captcha_resolvido = False

                    # Tenta reCAPTCHA v2 via 2captcha
                    if re.search(r'recaptcha|data-sitekey', html_cap, re.I):
                        site_key = await _extrair_site_key_recaptcha(page)
                        if site_key:
                            token = await _resolver_recaptcha_2captcha(url_manual, site_key)
                            if token:
                                await page.evaluate(f"""
                                    document.querySelectorAll('textarea[name="g-recaptcha-response"]').forEach(el => el.value = '{token}');
                                """)
                                captcha_resolvido = True

                    # Tenta CAPTCHA de imagem via 2captcha (seletores ampliados para portais PHP gov)
                    if not captcha_resolvido:
                        img_sel = page.locator(
                            'img[src*="captcha" i], img[id*="captcha" i], img[class*="captcha" i],'
                            'img[src*="gera_captcha" i], img[src*="codigo" i], img[src*="segur" i],'
                            'img[src*="imgseg" i], img[src*="verificacao" i], img[src*="verify" i],'
                            'img[src*="imagem" i][src*=".php"], img[src*="captcha.php"]'
                        ).first
                        if await img_sel.count() > 0:
                            try:
                                img_bytes = await img_sel.screenshot()
                                texto = await _resolver_captcha_imagem_2captcha(img_bytes)
                                if texto:
                                    # Seletores específicos do SEMEF/STM + genéricos gov.br
                                    _CAPTCHA_INPUTS = [
                                        'input[id*="captcha" i]', 'input[name*="captcha" i]',
                                        'input[placeholder*="captcha" i]',
                                        'input[name*="codigo" i]', 'input[id*="codigo" i]',
                                        'input[name*="segur" i]', 'input[id*="segur" i]',
                                        'input[name*="codigoSeg" i]', 'input[id*="codigoSeg" i]',
                                        'input[name*="txtCaptcha" i]', 'input[id*="txtCaptcha" i]',
                                        'input[name*="imgseg" i]', 'input[id*="imgseg" i]',
                                        'input[placeholder*="ódigo" i]', 'input[placeholder*="egur" i]',
                                    ]
                                    for csel in _CAPTCHA_INPUTS:
                                        el = page.locator(csel).first
                                        if await el.count() > 0:
                                            await el.fill(texto)
                                            captcha_resolvido = True
                                            break
                                    # Fallback: preenche o input de texto mais próximo da imagem do captcha
                                    if not captcha_resolvido:
                                        try:
                                            el = page.locator(f"input[type='text']:near(img[src*='captcha' i], img[src*='gera' i], img[src*='segur' i])").first
                                            if await el.count() > 0:
                                                await el.fill(texto)
                                                captcha_resolvido = True
                                        except Exception:
                                            pass
                                    # Último recurso: segundo input de texto na página (primeiro é o CNPJ)
                                    if not captcha_resolvido:
                                        try:
                                            inputs = page.locator('input[type="text"]')
                                            count = await inputs.count()
                                            for i in range(1, count):  # pula o primeiro (CNPJ)
                                                el = inputs.nth(i)
                                                val = await el.get_attribute("value") or ""
                                                if not val or not any(c.isdigit() for c in val):
                                                    await el.fill(texto)
                                                    captcha_resolvido = True
                                                    break
                                        except Exception:
                                            pass
                            except Exception:
                                pass

                    if not captcha_resolvido:
                        import os as _os
                        from ..config import settings as _cfg3
                        _key = _os.environ.get("DOIS_CAPTCHA_KEY") or _cfg3.DOIS_CAPTCHA_KEY
                        dica = (
                            "Configure DOIS_CAPTCHA_KEY no Railway para resolver automaticamente."
                            if not _key else
                            "2captcha acionado mas não resolveu o CAPTCHA do SEMEF. Tente novamente."
                        )
                        return {
                            "status": "em_analise",
                            "observacao": (
                                f"SEMEF Manaus exige CAPTCHA. {dica} "
                                f"Acesse: {url_manual} — CNPJ: {cnpj_digits}"
                            ),
                        }
                # Clica em Consultar
                for sel in ['input[type="submit"]', 'button:has-text("Consultar")',
                            'input[value*="Consultar" i]']:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click()
                            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                            await page.wait_for_timeout(2000)
                            break
                    except Exception:
                        pass
                html = await page.content()
                result = _parse_certidao_html(html, "cnd_municipal", page_url=page.url)
                if result["status"] == "regular":
                    result = await _capturar_pdf_certidao(page, result)
                if result["status"] == "em_analise":
                    result["observacao"] = (result.get("observacao") or "") + f" Acesse: {url_manual}"
                return result
            except Exception as e:
                return {
                    "status": "em_analise",
                    "observacao": f"Erro ao acessar SEMEF: {str(e)[:120]}. Acesse: {url_manual}",
                }

        # Tenta sem proxy primeiro — SEMEF é portal municipal, menos restritivo
        try:
            return await _run_playwright_no_cert(_tarefa_manaus, proxy_url=None)
        except Exception as e_noproxy:
            # Fallback com proxy se falhar sem proxy
            try:
                return await _run_playwright_no_cert(_tarefa_manaus)
            except Exception as e:
                return {"status": "em_analise", "observacao": f"Erro ao acessar SEMEF: {str(e)[:120]}. Acesse: {url_manual}"}

    # ── Outros municípios ─────────────────────────────────────────────────────
    _PORTAIS = {
        "são paulo": "https://nfe.prefeitura.sp.gov.br/contribuinte/certidao.aspx",
        "rio de janeiro": "https://mobuss.rio.rj.gov.br/certidao",
        "belo horizonte": "https://bhiss.pbh.gov.br/bhiss/certidao",
        "fortaleza": "https://sefin.fortaleza.ce.gov.br/certidao",
        "curitiba": "https://www.curitiba.pr.gov.br/servicos/certidao",
        "porto alegre": "https://prefeitura.poa.br/smf/certidao",
    }
    url = _PORTAIS.get(mun_key, "")
    if not url:
        return {
            "status": "em_analise",
            "observacao": f"Portal da Prefeitura de {municipio}/{uf} ainda não integrado. Consulte manualmente no site da prefeitura.",
        }

    if not await _playwright_ok():
        return {"status": "em_analise", "observacao": f"Playwright não disponível. Acesse: {url}"}

    async def _tarefa(page):
        await page.goto(url, wait_until="networkidle", timeout=40_000)
        for sel in ['input[name*="cnpj" i]', 'input[id*="cnpj" i]', 'input[type="text"]']:
            try:
                await page.fill(sel, cnpj_digits, timeout=3_000)
                break
            except Exception:
                pass
        for sel in ['button[type="submit"]', 'input[type="submit"]', 'input[type="image"]']:
            try:
                await page.click(sel, timeout=3_000)
                break
            except Exception:
                pass
        await page.wait_for_load_state("networkidle", timeout=40_000)
        return _parse_certidao_html(await page.content(), "cnd_municipal", page_url=page.url)

    try:
        return await _run_playwright_no_cert(_tarefa)
    except Exception as e:
        return {"status": "em_analise", "observacao": f"Erro na consulta prefeitura {municipio}: {e}. Acesse o portal da prefeitura."}


async def _consultar_cnd_falencia(cnpj: str, uf: str = "AM", razao_social: str = "") -> dict:
    """Certidão de Falência/Recuperação Judicial — emitida pelos Tribunais de Justiça estaduais.
    Para AM: fluxo completo TJAM (SAJ) — preenchimento automático + download via número do pedido.
    """
    cnpj_digits = re.sub(r'\D', '', cnpj)
    uf_upper = uf.upper()

    # ── TJAM — fluxo automatizado (número do pedido aparece na página após envio) ──
    if uf_upper == "AM":
        if not await _playwright_ok():
            return {"status": "em_analise",
                    "observacao": "Playwright não disponível. Acesse: https://consultasaj.tjam.jus.br/sco/abrirCadastro.do"}

        EMAIL_ESCRITORIO = "processos@mendeselimaconsultoria.com"
        # TJAM tem dois portais SAJ — tenta o consultasaj primeiro, depois esaj como fallback
        _TJAM_URLS_CADASTRO = [
            "https://consultasaj.tjam.jus.br/sco/abrirCadastro.do",
            "https://esaj.tjam.jus.br/sco/abrirCadastro.do",
        ]
        URL_CADASTRO = _TJAM_URLS_CADASTRO[0]
        URL_DOWNLOAD = "https://consultasaj.tjam.jus.br/sco/abrirDownload.do"

        async def _tarefa_tjam(page):
            # ── Passo 1: preenche o formulário de pedido — tenta as duas URLs ─
            carregou = False
            for _url_tjam in _TJAM_URLS_CADASTRO:
                try:
                    await page.goto(_url_tjam, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(2000)
                    titulo = await page.title()
                    if "404" not in titulo and len(await page.content()) > 500:
                        carregou = True
                        break
                except Exception:
                    pass
            if not carregou:
                return {
                    "status": "em_analise",
                    "observacao": (
                        "Portal TJAM inacessível (404 nos dois endpoints SAJ). "
                        "Acesse manualmente: https://consultasaj.tjam.jus.br/sco/abrirCadastro.do"
                    ),
                }
            await page.wait_for_timeout(1000)

            # Comarca: Manaus
            for sel in ['select[name*="comarca" i]', 'select[id*="comarca" i]', 'select']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.select_option(label="Manaus")
                        await page.wait_for_timeout(800)
                        break
                except Exception:
                    pass

            # Modelo: Falência e Recuperação de Crédito
            for sel in ['select[name*="modelo" i]', 'select[id*="modelo" i]',
                        'select[name*="assunto" i]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.select_option(label="Falência e Recuperação de Crédito")
                        await page.wait_for_timeout(500)
                        break
                except Exception:
                    try:
                        await el.select_option(value="F")
                        break
                    except Exception:
                        pass

            # Pessoa: Jurídica
            for sel in ['input[value="J"]', 'input[value="Juridica"]', 'input[value="JURIDICA"]',
                        'label:has-text("Jurídica") input', 'input[type="radio"]:nth-of-type(2)']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.check()
                        await page.wait_for_timeout(500)
                        break
                except Exception:
                    pass

            # Razão Social
            nome = razao_social.upper()[:100] if razao_social else ""
            for sel in ['input[name*="razao" i]', 'input[id*="razao" i]',
                        'input[name*="nome" i]', 'input[id*="nome" i]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(nome)
                        break
                except Exception:
                    pass

            # CNPJ
            for sel in ['input[name*="cnpj" i]', 'input[id*="cnpj" i]',
                        'input[name*="documento" i]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(cnpj_digits)
                        break
                except Exception:
                    pass

            # E-mail
            for sel in ['input[type="email"]', 'input[name*="email" i]', 'input[id*="email" i]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(EMAIL_ESCRITORIO)
                        break
                except Exception:
                    pass

            # Resolve reCAPTCHA via 2captcha (se chave configurada)
            await page.wait_for_timeout(2000)
            site_key = await _extrair_site_key_recaptcha(page)
            recaptcha_ok = False
            if site_key:
                token = await _resolver_recaptcha_2captcha(URL_CADASTRO, site_key)
                if token:
                    await page.evaluate(f"""
                        (() => {{
                            const inject = (v) => {{
                                document.querySelectorAll('textarea[name="g-recaptcha-response"]').forEach(el => el.value = v);
                                const hidden = document.getElementById('g-recaptcha-response');
                                if (hidden) hidden.value = v;
                            }};
                            inject('{token}');
                            // Tenta disparar callback do reCAPTCHA
                            try {{
                                const cfg = window.___grecaptcha_cfg;
                                if (cfg && cfg.clients) {{
                                    Object.values(cfg.clients).forEach(c => {{
                                        const cb = Object.values(c).find(v => v && typeof v.callback === 'function');
                                        if (cb) cb.callback('{token}');
                                    }});
                                }}
                            }} catch(e) {{}}
                        }})();
                    """)
                    await page.wait_for_timeout(1500)
                    recaptcha_ok = True

            if not recaptcha_ok:
                # Fallback: tenta clicar no checkbox (pode funcionar em testes simples)
                try:
                    for fr in page.frames:
                        if "recaptcha" in (fr.url or "").lower():
                            checkbox = fr.locator(".recaptcha-checkbox-border, #recaptcha-anchor")
                            if await checkbox.count() > 0:
                                await checkbox.click(timeout=5000)
                                await page.wait_for_timeout(4000)
                                break
                except Exception:
                    pass

            # Checkbox de confirmação (termos)
            for sel in ['input[type="checkbox"]']:
                try:
                    els = page.locator(sel)
                    count = await els.count()
                    for i in range(count):
                        el = els.nth(i)
                        id_attr = await el.get_attribute("id") or ""
                        if "captcha" not in id_attr.lower():
                            await el.check()
                except Exception:
                    pass

            # Clica em Enviar
            for sel in ['input[value="Enviar"]', 'input[value="ENVIAR"]',
                        'button:has-text("Enviar")', 'input[type="submit"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click()
                        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                        await page.wait_for_timeout(3000)
                        break
                except Exception:
                    pass

            # ── Passo 2: extrai Número e Data do Pedido da página de confirmação ──
            html_conf = await page.content()
            num_m = re.search(
                r'N[uú]mero\s+do\s+Pedido\s*[:\s]+(\d+)',
                html_conf, re.I | re.DOTALL
            )
            dat_m = re.search(
                r'Data\s+do\s+Pedido\s*[:\s]+(\d{2}/\d{2}/\d{4})',
                html_conf, re.I | re.DOTALL
            )

            if not num_m or not dat_m:
                title_m = re.search(r'<title[^>]*>([^<]+)</title>', html_conf, re.I)
                title = title_m.group(1).strip() if title_m else "(sem título)"
                from ..config import settings as _cfg2
                captcha_hint = (
                    "Configure a variável DOIS_CAPTCHA_KEY no Railway com sua chave do 2captcha.com "
                    "para resolver o reCAPTCHA automaticamente."
                    if not _cfg2.DOIS_CAPTCHA_KEY else
                    "O 2captcha foi acionado mas não obteve o token a tempo. Tente novamente."
                )
                return {
                    "status": "em_analise",
                    "observacao": (
                        f"TJAM: reCAPTCHA não resolvido (título: {title}). {captcha_hint} "
                        f"Ou acesse manualmente: {URL_CADASTRO}"
                    ),
                }

            numero_pedido = num_m.group(1).strip()
            data_pedido = dat_m.group(1).strip()

            # ── Passo 3: faz o download da certidão ──────────────────────────
            await page.goto(URL_DOWNLOAD, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)

            for sel in ['input[name*="numero" i]', 'input[id*="numero" i]',
                        'input[type="text"]:nth-of-type(1)']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(numero_pedido)
                        break
                except Exception:
                    pass

            for sel in ['input[name*="data" i]', 'input[id*="data" i]',
                        'input[type="text"]:nth-of-type(2)']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.fill(data_pedido)
                        break
                except Exception:
                    pass

            for sel in ['input[value="Consultar"]', 'button:has-text("Consultar")',
                        'input[type="submit"]']:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click()
                        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                        await page.wait_for_timeout(3000)
                        break
                except Exception:
                    pass

            html_cert = await page.content()
            result = _parse_certidao_html(html_cert, "cnd_falencia", page_url=page.url)
            # Usa o número do pedido como número da certidão se parser não achou
            if not result.get("numero_certidao"):
                result["numero_certidao"] = numero_pedido
            if not result.get("observacao"):
                result["observacao"] = f"Pedido TJAM nº {numero_pedido} de {data_pedido}"
            return result

        try:
            return await _run_playwright_no_cert(_tarefa_tjam)
        except Exception as e:
            return {
                "status": "em_analise",
                "observacao": f"TJAM: {str(e)[:200]}. Acesse: {URL_CADASTRO}",
            }

    # ── Outros estados ────────────────────────────────────────────────────────
    _TJ_URLS = {
        "SP": "https://esaj.tjsp.jus.br/sco/abrirCadastro.do",
        "RJ": "https://certidaodigital.tjrj.jus.br/certidaodigital/",
        "MG": "https://www4.tjmg.jus.br/juridico/sf/certidao.jsp",
        "RS": "https://www.tjrs.jus.br/site/processos/certidoes/",
        "PR": "https://projudi.tjpr.jus.br/projudi/",
        "SC": "https://www.tjsc.jus.br/web/guest/consulta-certidoes",
        "BA": "https://www.tjba.jus.br/portal/certidao",
        "CE": "https://esaj.tjce.jus.br/sco/abrirCadastro.do",
        "PE": "https://www.tjpe.jus.br/web/guest/servicos/certidoes",
        "GO": "https://projudi.tjgo.jus.br/BuscaCertidao",
        "MT": "https://www.tjmt.jus.br/servicos/certidoes",
        "MS": "https://www.tjms.jus.br/servicos/certidao",
        "PA": "https://www.tjpa.jus.br/portalExterno/iniciarProcesso",
    }
    url = _TJ_URLS.get(uf_upper, "")
    if not url:
        return {
            "status": "em_analise",
            "observacao": f"Certidão de falência do TJ{uf_upper}: consulte manualmente em www.tj{uf.lower()}.jus.br",
        }

    import httpx as _httpx
    headers = {"User-Agent": "Mozilla/5.0 Chrome/125.0.0.0"}
    async with _httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True) as c:
        try:
            r = await c.post(url, data={"cnpj": cnpj_digits}, headers=headers)
            html = r.text
        except Exception:
            html = ""
    result = _parse_certidao_html(html, "cnd_falencia", page_url=url)
    if result["status"] == "em_analise":
        result["observacao"] = (result.get("observacao") or "") + f" Acesse: {url}"
    return result


# ─── Parsers ─────────────────────────────────────────────────────────────────

def _parse_mit_lxml(html: str, ano: int, mes_fixo: Optional[int] = None) -> list:
    """
    Parser específico para o MIT da DCTFWeb.

    O MIT exibe a receita bruta por competência em formato de tabela.
    Colunas típicas: Competência | Receita Bruta | Deduções | Base de Cálculo | IRPJ | CSLL

    Extrai a coluna "Receita Bruta" ou "Receita" para cada competência do ano.
    """
    receitas: list[dict] = []

    try:
        from lxml import html as lx
        tree = lx.fromstring(html)

        for table in tree.iter("table"):
            headers = [
                " ".join(th.text_content().split()).upper()
                for th in table.iter("th")
            ]

            # Identifica índice da coluna de Receita Bruta
            receita_idx = None
            comp_idx = None
            for i, h in enumerate(headers):
                if "RECEITA BRUTA" in h or ("RECEITA" in h and "BRUTA" in h):
                    receita_idx = i
                if "COMPETÊNCIA" in h or "COMPETENCIA" in h or "PERÍODO" in h or "PERIODO" in h:
                    comp_idx = i

            rows = [r for r in table.iter("tr") if list(r.iter("td"))]
            for row in rows:
                cells = [" ".join(td.text_content().split()) for td in row.iter("td")]
                if not cells:
                    continue

                row_text = " ".join(cells)

                # Extrai competência MM/AAAA
                comp_m = re.search(r'(\d{2})/(\d{4})', row_text)
                if not comp_m:
                    continue
                mes_s, ano_s = comp_m.group(1), comp_m.group(2)
                if int(ano_s) != ano:
                    continue
                if mes_fixo and int(mes_s) != mes_fixo:
                    continue

                # Prefere coluna de Receita Bruta se mapeada
                valor_str = None
                if receita_idx is not None and receita_idx < len(cells):
                    valor_str = cells[receita_idx]
                else:
                    # Heurística: pega o maior valor numérico na linha
                    # (geralmente a receita bruta é o maior número antes das deduções)
                    vals = re.findall(r'[\d]{1,3}(?:\.[\d]{3})*,[\d]{2}', row_text)
                    if vals:
                        valor_str = max(vals, key=lambda v: float(v.replace(".", "").replace(",", ".")))

                if not valor_str:
                    continue

                val_clean = re.sub(r"[^\d,.]", "", valor_str).replace(".", "").replace(",", ".")
                try:
                    valor = float(val_clean)
                    if valor <= 0:
                        continue
                    comp = f"{ano_s}-{mes_s.zfill(2)}"
                    if not any(r["competencia"] == comp for r in receitas):
                        receitas.append({"competencia": comp, "valor_receita": valor, "origem": "dctfweb_mit"})
                except ValueError:
                    pass

    except ImportError:
        pass

    # Fallback regex: procura padrão "MM/AAAA ... R$ valor" ou "valor" próximo de competência
    if not receitas:
        pat = re.compile(
            r'(\d{2})/(\d{4})[^<\n]{0,300}?'
            r'(?:Receita\s+Bruta[^<\n]{0,50})?'
            r'([\d]{1,3}(?:\.[\d]{3})*,[\d]{2})',
            re.DOTALL | re.IGNORECASE,
        )
        for m in pat.finditer(html):
            mes_s, ano_s, val_s = m.group(1), m.group(2), m.group(3)
            if int(ano_s) != ano:
                continue
            if mes_fixo and int(mes_s) != mes_fixo:
                continue
            val = val_s.replace(".", "").replace(",", ".")
            try:
                comp = f"{ano_s}-{mes_s.zfill(2)}"
                if not any(r["competencia"] == comp for r in receitas):
                    receitas.append({"competencia": comp, "valor_receita": float(val), "origem": "dctfweb_mit"})
            except ValueError:
                pass

    return receitas


def _parse_pgdas_lxml(html: str, ano: int, mes_fixo: Optional[int] = None) -> list:
    """
    Extrai receita bruta do HTML do PGDAS-D usando lxml para tabelas
    e regex como fallback. Muito mais robusto que regex puro.
    """
    receitas: list[dict] = []

    # ── Estratégia 1: lxml table parsing ──────────────────────────────────
    try:
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html)

        # Coleta texto de todas as células de todas as tabelas
        for table in tree.iter("table"):
            rows = list(table.iter("tr"))

            # Detecta colunas pelo cabeçalho para saber qual é competência vs receita bruta
            header_cells: list[str] = []
            comp_col = val_col = -1
            for row in rows[:5]:  # verifica só as primeiras linhas buscando cabeçalho
                ths = [" ".join(c.text_content().split()).upper() for c in row.iter("th", "td")]
                for i, th in enumerate(ths):
                    if comp_col < 0 and re.search(r'COMPET|PERÍODO|PERIODO', th):
                        comp_col = i
                    if val_col < 0 and re.search(r'RECEITA\s*BRUTA|FATURAMENTO|VALOR\s*TOTAL|VALOR\s*DA\s*RECEITA', th):
                        val_col = i
                if comp_col >= 0 and val_col >= 0:
                    header_cells = ths
                    break

            for row in rows:
                cells = [" ".join(td.text_content().split()) for td in row.iter("td", "th")]
                row_text = " ".join(cells)

                # Procura competência MM/AAAA
                if comp_col >= 0 and comp_col < len(cells):
                    comp_m = re.search(r'(\d{2})/(\d{4})', cells[comp_col])
                else:
                    comp_m = re.search(r'(\d{2})/(\d{4})', row_text)
                if not comp_m:
                    continue
                mes_str, ano_str = comp_m.group(1), comp_m.group(2)
                if int(ano_str) != ano:
                    continue
                if mes_fixo and int(mes_str) != mes_fixo:
                    continue

                # Procura valor R$
                search_text = cells[val_col] if val_col >= 0 and val_col < len(cells) else row_text
                val_m = re.search(r'R\$\s*([\d.,]+)', search_text)
                if not val_m:
                    val_m = re.search(r'([\d]{1,3}(?:\.[\d]{3})+,[\d]{2})', search_text)
                if not val_m and val_col < 0:
                    # Tenta qualquer número >=1000 na linha (receita bruta mínima esperada)
                    for num_m in re.finditer(r'([\d]{1,3}(?:\.[\d]{3})*,[\d]{2})', row_text):
                        try:
                            v = float(num_m.group(1).replace(".", "").replace(",", "."))
                            if v >= 1000:
                                val_m = num_m
                                break
                        except ValueError:
                            pass
                if not val_m:
                    continue

                val_str = val_m.group(1).replace(".", "").replace(",", ".")
                try:
                    valor = float(val_str)
                    if valor <= 0:
                        continue
                    comp = f"{ano_str}-{mes_str.zfill(2)}"
                    if not any(r["competencia"] == comp for r in receitas):
                        receitas.append({"competencia": comp, "valor_receita": valor, "origem": "pgdas_d"})
                except ValueError:
                    pass

    except ImportError:
        pass  # lxml não disponível, cai no regex

    # ── Estratégia 2: regex fallback ──────────────────────────────────────
    if not receitas:
        patterns = [
            re.compile(r'(\d{2})/(\d{4})[^<]{0,200}?R\$\s*([\d.,]+)', re.DOTALL),
            re.compile(r'(\d{4})-(\d{2})[^<]{0,200}?R\$\s*([\d.,]+)', re.DOTALL),
        ]
        for pat in patterns:
            for m in pat.finditer(html):
                g = m.groups()
                if len(g) == 3:
                    if "/" in m.group(0)[:7]:
                        mes_s, ano_s, val_s = g
                    else:
                        ano_s, mes_s, val_s = g
                    if int(ano_s) != ano:
                        continue
                    if mes_fixo and int(mes_s) != mes_fixo:
                        continue
                    val = val_s.replace(".", "").replace(",", ".")
                    try:
                        comp = f"{ano_s}-{mes_s.zfill(2)}"
                        if not any(r["competencia"] == comp for r in receitas):
                            receitas.append({"competencia": comp, "valor_receita": float(val), "origem": "pgdas_d"})
                    except ValueError:
                        pass

    # ── Estratégia 3: extrair RBT12 do texto livre (campos fora de tabela) ──
    if not receitas:
        # O PGDAS-D exibe "RBT12: R$ 123.456,00" ou "Receita Bruta dos Últimos 12 Meses: R$ ..."
        rbt12_pats = [
            re.compile(r'rbt12[^R]{0,30}R\$\s*([\d.,]+)', re.IGNORECASE),
            re.compile(r'receita\s+bruta\s+(?:acumulada|dos\s+últimos|total)\s+(?:12|doze)[^R]{0,30}R\$\s*([\d.,]+)', re.IGNORECASE),
            re.compile(r'receita\s+bruta\s+nos\s+12\s+meses[^R]{0,30}R\$\s*([\d.,]+)', re.IGNORECASE),
            re.compile(r'RBT12[^0-9]{0,10}([\d]{1,3}(?:\.[\d]{3})+,[\d]{2})', re.IGNORECASE),
        ]
        for pat in rbt12_pats:
            m = pat.search(html)
            if m:
                try:
                    val = float(m.group(1).replace(".", "").replace(",", "."))
                    if val > 0:
                        # Usar competência do mês anterior ao atual como referência
                        from datetime import date as _date
                        hoje = _date.today()
                        mes_ref = hoje.month - 1 if hoje.month > 1 else 12
                        ano_ref = hoje.year if hoje.month > 1 else hoje.year - 1
                        comp = f"{ano_ref}-{mes_ref:02d}"
                        receitas.append({"competencia": comp, "valor_receita": val, "origem": "pgdas_d_rbt12"})
                        break
                except ValueError:
                    pass

    return receitas


def _parse_esocial_lxml(html: str, ano: int) -> list:
    """Extrai totais de folha do HTML do eSocial usando lxml + regex."""
    folhas: list[dict] = []

    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(html)

        for table in tree.iter("table"):
            rows = list(table.iter("tr"))
            comp_col = val_col = -1
            for row in rows[:5]:
                ths = [" ".join(c.text_content().split()).upper() for c in row.iter("th", "td")]
                for i, th in enumerate(ths):
                    if comp_col < 0 and re.search(r'COMPET|PERÍODO|PERIODO|MÊS|MES', th):
                        comp_col = i
                    if val_col < 0 and re.search(r'SALÁRIO|SALARIO|FOLHA|REMUNER|TOTAL', th):
                        val_col = i
                if comp_col >= 0 and val_col >= 0:
                    break

            for row in rows:
                cells = [" ".join(td.text_content().split()) for td in row.iter("td", "th")]
                row_text = " ".join(cells)
                search_comp = cells[comp_col] if comp_col >= 0 and comp_col < len(cells) else row_text
                comp_m = re.search(r'(\d{2})/(\d{4})', search_comp)
                if not comp_m:
                    continue
                mes_s, ano_s = comp_m.group(1), comp_m.group(2)
                if int(ano_s) != ano:
                    continue
                search_val = cells[val_col] if val_col >= 0 and val_col < len(cells) else row_text
                val_m = re.search(r'R\$\s*([\d.,]+)', search_val)
                if not val_m:
                    val_m = re.search(r'([\d]{1,3}(?:\.[\d]{3})+,[\d]{2})', search_val)
                if not val_m:
                    for num_m in re.finditer(r'([\d]{1,3}(?:\.[\d]{3})*,[\d]{2})', row_text):
                        try:
                            v = float(num_m.group(1).replace(".", "").replace(",", "."))
                            if v >= 100:
                                val_m = num_m
                                break
                        except ValueError:
                            pass
                if not val_m:
                    continue
                val = val_m.group(1).replace(".", "").replace(",", ".")
                try:
                    comp = f"{ano_s}-{mes_s.zfill(2)}"
                    if not any(f["competencia"] == comp for f in folhas):
                        folhas.append({"competencia": comp, "valor_total_folha": float(val)})
                except ValueError:
                    pass
    except ImportError:
        pass

    if not folhas:
        pat = re.compile(r'(\d{2})/(\d{4})[^<]{0,200}?R\$\s*([\d.,]+)', re.DOTALL)
        for m in pat.finditer(html):
            mes_s, ano_s, val_s = m.group(1), m.group(2), m.group(3)
            if int(ano_s) != ano:
                continue
            val = val_s.replace(".", "").replace(",", ".")
            try:
                comp = f"{ano_s}-{mes_s.zfill(2)}"
                if not any(f["competencia"] == comp for f in folhas):
                    folhas.append({"competencia": comp, "valor_total_folha": float(val)})
            except ValueError:
                pass

    return folhas


async def _capturar_pdf_certidao(page, result: dict) -> dict:
    """
    Após detectar certidão Regular, tenta capturar o PDF que o portal abre
    numa nova aba ou via download direto. Retorna result enriquecido com pdf_temp_path.
    """
    context = page.context
    novas_abas: list = []
    context.on("page", lambda p: novas_abas.append(p))

    # Seletores de botões de download/impressão comuns nos portais brasileiros
    _SELS_PDF = [
        'a:has-text("Emitir")', 'button:has-text("Emitir")',
        'a:has-text("PDF")', 'button:has-text("PDF")',
        'a:has-text("Imprimir")', 'button:has-text("Imprimir")',
        'a:has-text("Baixar")', 'button:has-text("Baixar")',
        'a:has-text("Download")', 'a[href*=".pdf" i]',
        'input[type="image"]',
    ]
    for sel in _SELS_PDF:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=5_000)
                await page.wait_for_timeout(5_000)
                break
        except Exception:
            pass

    # Verifica se abriu nova aba com PDF
    for nova in novas_abas:
        try:
            await nova.wait_for_load_state("load", timeout=15_000)
            url_nova = nova.url
            if url_nova and url_nova.startswith("http"):
                result.setdefault("observacao", "")
                # Tenta baixar os bytes da nova aba
                body = await nova.evaluate("() => document.body ? document.body.innerText : ''")
                # Acessa os bytes da resposta através de fetch
                try:
                    import httpx as _httpx
                    async with _httpx.AsyncClient(
                        verify=False, timeout=20, follow_redirects=True,
                        headers={"User-Agent": "Mozilla/5.0 Chrome/125.0.0.0"},
                    ) as hx:
                        r = await hx.get(url_nova)
                        if r.status_code == 200 and len(r.content) > 1000:
                            tmp = tempfile.mktemp(suffix=".pdf")
                            with open(tmp, "wb") as f:
                                f.write(r.content)
                            result["pdf_temp_path"] = tmp
                            break
                except Exception:
                    pass
        except Exception:
            pass

    # Tenta download via Playwright se a página atual abrir download
    if "pdf_temp_path" not in result:
        try:
            async with page.expect_download(timeout=8_000) as dl_info:
                for sel in _SELS_PDF:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click(timeout=3_000)
                            break
                    except Exception:
                        pass
            dl = await dl_info.value
            tmp = tempfile.mktemp(suffix=".pdf")
            await dl.save_as(tmp)
            result["pdf_temp_path"] = tmp
        except Exception:
            pass

    return result


def _parse_certidao_html(html: str, tipo: str, page_url: str = "") -> dict:
    """Extrai status e validade de uma certidão a partir do HTML retornado pelo portal."""
    html_up = html.upper()

    # Determina se o HTML tem conteúdo real ou é apenas shell JS
    html_text_len = len(re.sub(r'<[^>]+>', '', html).strip())

    # Determina status
    obs_auto = None
    if any(k in html_up for k in ["NEGATIVA COM EFEITO DE POSITIVA", "POSITIVA COM EFEITO DE NEGATIVA"]):
        status = "regular"
    elif any(k in html_up for k in [
        "CERTIDÃO NEGATIVA", "CERTIDAO NEGATIVA", "NEGATIVA DE DÉBITO", "NEGATIVA DE DEBITO",
        "NADA CONSTA", "SEM DÉBITO", "SEM DEBITO", "REGULARIDADE FISCAL", "SITUAÇÃO REGULAR",
        "SITUACAO REGULAR", "CERTIDÃO DE REGULARIDADE", "CERTIDAO DE REGULARIDADE",
        "CERTIDÃO POSITIVA COM EFEITO DE NEGATIVA", "NENHUM DÉBITO", "NENHUM DEBITO",
    ]):
        status = "regular"
    elif any(k in html_up for k in ["NEGATIVA", "REGULAR"]) and not any(
            k in html_up for k in ["POSITIVA", "IRREGULAR", "DÉBITO", "DEBITO", "PENDÊNCIA", "PENDENCIA"]):
        status = "regular"
    elif any(k in html_up for k in [
        "POSITIVA", "DÉBITOS", "DEBITOS", "IRREGUL", "PENDÊNCIA", "PENDENCIA", "DEVEDOR",
        "EM ABERTO", "NÃO REGULAR", "NAO REGULAR", "EMISSÃO IMPEDIDA", "EMISSAO IMPEDIDA",
    ]):
        status = "irregular"
    elif any(k in html_up for k in ["PROCESSANDO", "AGUARDANDO", "ANÁLISE", "ANALISE", "EM PROCESSAMENTO"]):
        status = "em_analise"
    elif html_text_len < 200:
        # Página em branco ou apenas JS — Playwright pode não ter esperado render completo
        status = "em_analise"
        obs_auto = f"Portal retornou página com pouco conteúdo ({html_text_len} chars). Tente novamente ou acesse manualmente."
    else:
        # HTML tem conteúdo mas não reconhecemos keywords — pode ser CAPTCHA, login ou layout novo
        status = "em_analise"
        title_m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        title = title_m.group(1).strip() if title_m else "(sem título)"
        obs_auto = f"Portal respondeu (título: {title}) mas keywords de certidão não foram encontradas. Verifique manualmente."
        if page_url:
            obs_auto += f" URL: {page_url}"

    # Extrai data de validade — prioriza padrões específicos e datas futuras
    hoje = date.today()
    data_validade = None
    data_emissao = None
    candidatas: list[date] = []
    emissoes: list[date] = []

    _MESES_PT = {
        'janeiro': 1, 'fevereiro': 2, 'marco': 3, 'março': 3,
        'abril': 4, 'maio': 5, 'junho': 6, 'julho': 7,
        'agosto': 8, 'setembro': 9, 'outubro': 10,
        'novembro': 11, 'dezembro': 12,
    }

    def _parse_data_escrita(txt: str) -> list[date]:
        """Extrai datas no formato 'DD de Mês de AAAA'."""
        datas = []
        for m in re.finditer(
            r'(\d{1,2})\s+de\s+(janeiro|fevereiro|mar[çc]o|abril|maio|junho|julho|'
            r'agosto|setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})',
            txt, re.IGNORECASE,
        ):
            mes_key = m.group(2).lower().replace('ç', 'c')
            mes = _MESES_PT.get(mes_key)
            if mes:
                try:
                    datas.append(date(int(m.group(3)), mes, int(m.group(1))))
                except ValueError:
                    pass
        return datas

    # Padrões prioritários (contexto de validade)
    for pat in [
        r'v[aá]lid[ao]\s+(?:at[eé]|até)\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'validade\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'expira\s+em\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'prazo\s+de\s+validade\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'data\s+de\s+validade\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'v[aá]lida\s+(?:at[eé]|até)\s+(\d{2}/\d{2}/\d{4})',
        r'(\d{2}/\d{2}/\d{4})\s*(?:\(validade|\(prazo)',
    ]:
        for m in re.finditer(pat, html, re.IGNORECASE):
            try:
                candidatas.append(datetime.strptime(m.group(1), "%d/%m/%Y").date())
            except ValueError:
                pass

    # Datas escritas no contexto de validade ("válida até 23 de dezembro de 2026")
    for m in re.finditer(
        r'v[aá]lid[ao]\s+(?:at[eé]|até)[^.]{0,10}'
        r'(\d{1,2})\s+de\s+(janeiro|fevereiro|mar[çc]o|abril|maio|junho|julho|'
        r'agosto|setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})',
        html, re.IGNORECASE,
    ):
        mes_key = m.group(2).lower().replace('ç', 'c')
        mes = _MESES_PT.get(mes_key)
        if mes:
            try:
                candidatas.append(date(int(m.group(3)), mes, int(m.group(1))))
            except ValueError:
                pass

    # Padrões de data de emissão (usados para calcular validade padrão)
    for pat in [
        r'emitida\s+em\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'emiss[aã]o\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'expedida\s+em\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'data\s+(?:de\s+)?emiss[aã]o\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'gerada\s+em\s*:?\s*(\d{2}/\d{2}/\d{4})',
    ]:
        for m in re.finditer(pat, html, re.IGNORECASE):
            try:
                emissoes.append(datetime.strptime(m.group(1), "%d/%m/%Y").date())
            except ValueError:
                pass

    # Datas de emissão escritas por extenso
    for pat in [
        r'emitida?\s+em[^.]{0,5}(\d{1,2})\s+de\s+(janeiro|fevereiro|mar[çc]o|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})',
        r'expedida\s+em[^.]{0,5}(\d{1,2})\s+de\s+(janeiro|fevereiro|mar[çc]o|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\s+de\s+(\d{4})',
    ]:
        for m in re.finditer(pat, html, re.IGNORECASE):
            mes_key = m.group(2).lower().replace('ç', 'c')
            mes = _MESES_PT.get(mes_key)
            if mes:
                try:
                    emissoes.append(date(int(m.group(3)), mes, int(m.group(1))))
                except ValueError:
                    pass

    # Prioriza datas futuras; se não houver, usa a mais recente
    futuras = [d for d in candidatas if d > hoje]
    if futuras:
        data_validade = min(futuras)
    elif candidatas:
        data_validade = max(candidatas)

    _VALIDADE_DIAS = {
        "cnd_federal": 180, "cndt_tst": 180, "cndt_trt": 180,
        "cnd_fgts": 90, "cnd_estadual": 180, "cnd_estadual_nc": 180,
        "cnd_municipal": 180, "cnd_falencia": 90,
    }
    from datetime import timedelta as _td

    # Se não achou data de validade mas achou emissão → calcula pela regra legal
    if not data_validade and emissoes and status == "regular":
        dias = _VALIDADE_DIAS.get(tipo, 180)
        data_emissao = max(emissoes)
        data_validade = data_emissao + _td(days=dias)

    # Se Regular mas não achou nem emissão → usa hoje como emissão
    if not data_validade and status == "regular":
        dias = _VALIDADE_DIAS.get(tipo, 180)
        data_validade = hoje + _td(days=dias)

    # Extrai número da certidão
    _EXTENSOES = ('.js', '.css', '.html', '.htm', '.php', '.asp', '.json', '.png', '.jpg')
    numero = None
    for pat in [
        # Formatos específicos primeiro (CNDT, CND, CRF)
        r'\bCNDT\s*[-:]?\s*([\d]{10,20}[-/]?\d*)',
        r'\bCND\s*[-:]?\s*([\d]{6,20}[-/]?\d*)',
        r'\bCRF\s*[-:]?\s*([\d]{6,20}[-/]?\d*)',
        # Genéricos
        r'n[uú]mero\s*(?:da\s+certid[aã]o)?\s*:?\s*([A-Z0-9][A-Z0-9\-/.]{4,39})',
        r'certid[aã]o\s+n[oº°.]\s*:?\s*([A-Z0-9][A-Z0-9\-/.]{4,39})',
        r'c[oó]digo\s+(?:de\s+controle|de\s+verifica[cç][aã]o)\s*:?\s*([A-Z0-9][A-Z0-9\-]{5,29})',
        r'protocolo\s*:?\s*([A-Z0-9][A-Z0-9\-]{5,29})',
    ]:
        for m in re.finditer(pat, html, re.IGNORECASE):
            n = m.group(1).strip().rstrip('.')
            if not re.search(r'\d', n):
                continue
            if not any(n.lower().endswith(ext) for ext in _EXTENSOES):
                if not re.search(r'[a-f0-9]{8}-[a-f0-9]{4}', n.lower()):
                    numero = n
                    break
        if numero:
            break

    return {
        "status": status,
        "data_validade": data_validade,
        "numero_certidao": numero,
        "observacao": obs_auto,
    }
