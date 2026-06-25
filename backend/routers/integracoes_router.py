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
    """Busca receita bruta no PGDAS-D usando certificado A1 do cliente."""
    cliente = await _get_cliente(cliente_id, escritorio.id, db)
    cert_pem, key_pem = await _cert_ou_erro(cliente, senha)

    if not await _playwright_ok():
        raise HTTPException(503, "Módulo de automação não disponível.")

    try:
        resultado = await _run_playwright_multi(
            cert_pem, key_pem,
            ["https://www8.receita.fazenda.gov.br", "https://cav.receita.fazenda.gov.br"],
            lambda p, c: _tarefa_pgdas(p, c, cliente.cnpj, ano),
        )
        # Salva receitas no banco
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
    """Busca dados de folha no eSocial usando certificado A1 do cliente."""
    cliente = await _get_cliente(cliente_id, escritorio.id, db)
    cert_pem, key_pem = await _cert_ou_erro(cliente, senha)

    if not await _playwright_ok():
        raise HTTPException(503, "Módulo de automação não disponível.")

    try:
        resultado = await _run_playwright_multi(
            cert_pem, key_pem,
            ["https://empregador.esocial.gov.br", "https://login.esocial.gov.br",
             "https://cav.receita.fazenda.gov.br"],
            lambda p, c: _tarefa_esocial(p, c, cliente.cnpj, ano),
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
        return await _consultar_cnd_fgts(cnpj)
    if tipo == "cndt_tst":
        return await _consultar_cndt_tst(cnpj)
    if tipo == "cndt_trt":
        return await _consultar_cndt_trt(cnpj, uf)
    if tipo in ("cnd_estadual", "cnd_estadual_nc"):
        return await _consultar_cnd_estadual(cnpj, uf, tipo)
    if tipo == "cnd_municipal":
        return await _consultar_cnd_municipal(cnpj, cliente.municipio or "", uf)
    if tipo == "cnd_falencia":
        return await _consultar_cnd_falencia(cnpj, uf)
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


async def _run_playwright_multi(cert_pem: bytes, key_pem: bytes, origins: list[str], tarefa_fn) -> dict:
    """Igual a _run_playwright mas registra o certificado em múltiplas origens."""
    from playwright.async_api import async_playwright

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as cf:
        cf.write(cert_pem); cert_path = cf.name
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as kf:
        kf.write(key_pem); key_path = kf.name

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            certs = [{"origin": o, "certPath": cert_path, "keyPath": key_path} for o in origins]
            context = await browser.new_context(
                client_certificates=certs,
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
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


async def _run_playwright_no_cert(tarefa_fn) -> dict:
    """Playwright sem certificado cliente — para portais públicos (CND Federal, TST, FGTS)."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
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
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                client_certificates=[{"origin": origin, "certPath": cert_path, "keyPath": key_path}],
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
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

async def _tarefa_pgdas(page, context, cnpj: str, ano: int) -> dict:
    """
    Navega no portal do Simples Nacional e extrai receita bruta por competência.
    Tenta múltiplas estratégias de navegação para cobrir variações do portal.
    """
    cnpj_limpo = re.sub(r"\D", "", cnpj)
    receitas: list[dict] = []

    # 1. Autentica via e-CAC primeiro (certificado é apresentado aqui)
    await page.goto(
        "https://cav.receita.fazenda.gov.br/autenticacao/login",
        wait_until="domcontentloaded",
        timeout=_PGDAS_TIMEOUT,
    )
    await page.wait_for_timeout(3000)

    # Seleciona "Certificado Digital" se houver opção
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

    # 2. Navega para o Simples Nacional após autenticação
    await page.goto(
        "https://www8.receita.fazenda.gov.br/SimplesNacional/",
        wait_until="domcontentloaded",
        timeout=_PGDAS_TIMEOUT,
    )
    await page.wait_for_timeout(3000)

    # 3. Seleção de CNPJ (certificado multiempresa)
    try:
        el = page.locator(f"a:has-text('{cnpj_limpo[:8]}')").first
        if await el.count() > 0:
            await el.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
    except Exception:
        pass

    # 4. Navega pelos links do menu até encontrar dados
    menu_links = [
        "a:has-text('PGDAS')", "a:has-text('PGDAS-D')",
        "a:has-text('Consulta')", "a:has-text('Declarações')",
        "a:has-text('Extrato')", "a:has-text('Apuração')",
    ]
    for sel in menu_links:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_timeout(2000)
                html = await page.content()
                receitas = _parse_pgdas_lxml(html, ano)
                if receitas:
                    return {"receitas": receitas, "aviso": f"Dados extraídos de {len(receitas)} competência(s) — {ano}."}
        except Exception:
            pass

    # 4. Tenta acessar diretamente o PGDAS-D e navegar pelo calendário de competências
    await page.goto(
        "https://www8.receita.fazenda.gov.br/SimplesNacional/Aplicacoes/ATBHE/pgdas.app/emPGDAS/",
        wait_until="domcontentloaded",
        timeout=_PGDAS_TIMEOUT,
    )
    await page.wait_for_timeout(3000)

    # 4a. Tenta selecionar o ano via dropdown/select
    try:
        sel_ano = page.locator(f"select option[value='{ano}']").first
        if await sel_ano.count() > 0:
            parent = page.locator(f"select:has(option[value='{ano}'])").first
            await parent.select_option(str(ano))
            await page.wait_for_timeout(2000)
    except Exception:
        pass

    # 4b. Itera pelos meses: procura links/td com "MM/AAAA" e clica
    for mes in range(1, 13):
        comp_slash = f"{mes:02d}/{ano}"
        comp_key = f"{ano}-{mes:02d}"
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
                    for r in month_recs:
                        if not any(x["competencia"] == r["competencia"] for x in receitas):
                            receitas.append(r)
                await page.go_back()
                await page.wait_for_timeout(1500)
        except Exception:
            pass

    # 5. Parse da página atual (pode já ter tabela consolidada)
    if not receitas:
        html = await page.content()
        receitas = _parse_pgdas_lxml(html, ano)

    # 6. Tenta URL alternativa de extrato/consulta — aguarda JS renderizar
    if not receitas:
        for extrato_url in [
            f"https://www8.receita.fazenda.gov.br/SimplesNacional/Aplicacoes/ATBHE/pgdas.app/extrato.app/?ano={ano}",
            f"https://www8.receita.fazenda.gov.br/SimplesNacional/Aplicacoes/ATBHE/pgdas.app/historico.app/?ano={ano}",
        ]:
            try:
                await page.goto(extrato_url, wait_until="networkidle", timeout=45000)
                # Aguarda elemento de tabela ou competência aparecer
                try:
                    await page.wait_for_selector("table, .competencia, td, [class*='declara']",
                                                  timeout=8000)
                except Exception:
                    pass
                await page.wait_for_timeout(2000)
                html = await page.content()
                receitas = _parse_pgdas_lxml(html, ano)
                if receitas:
                    break
            except Exception:
                pass

    # 7. Última tentativa: itera clicando em links de competência na página atual
    if not receitas:
        try:
            html = await page.content()
            # Procura links com padrão MM/AAAA ou MM-AAAA no href ou texto
            from lxml import html as lx
            tree = lx.fromstring(html)
            comp_links = []
            for a in tree.iter("a"):
                txt = (a.text_content() or "").strip()
                href = a.get("href", "")
                if re.search(r'\d{2}/\d{4}|\d{2}-\d{4}', txt) or "competencia" in href.lower():
                    comp_links.append(txt)
            if comp_links:
                # Encontrou links — tenta clicar no primeiro mês disponível
                for mes in range(1, 13):
                    comp_slash = f"{mes:02d}/{ano}"
                    try:
                        el = page.locator(f"a:has-text('{comp_slash}')").first
                        if await el.count() > 0:
                            await el.click()
                            await page.wait_for_load_state("networkidle", timeout=20000)
                            await page.wait_for_timeout(1500)
                            html = await page.content()
                            month_recs = _parse_pgdas_lxml(html, ano, mes_fixo=mes)
                            for r2 in month_recs:
                                if not any(x["competencia"] == r2["competencia"] for x in receitas):
                                    receitas.append(r2)
                            await page.go_back()
                            await page.wait_for_timeout(1000)
                    except Exception:
                        pass
        except Exception:
            pass

    debug_url = page.url  # URL atual após toda navegação
    aviso = (
        f"Dados extraídos: {len(receitas)} competência(s) de {ano}."
        if receitas
        else (
            f"Acesso ao PGDAS-D estabelecido (URL final: {debug_url}). "
            f"Nenhuma declaração de {ano} encontrada — pode ser que o ano ainda não tenha "
            "declarações transmitidas, ou o portal exige navegação manual adicional."
        )
    )
    return {"receitas": receitas, "aviso": aviso}


# ─── Tarefa eSocial ──────────────────────────────────────────────────────────

async def _tarefa_esocial(page, context, cnpj: str, ano: int) -> dict:
    """
    Navega no portal do eSocial e extrai totais de folha de pagamento (S-5011).
    Tenta o novo portal empregador.esocial.gov.br e o portal legado.
    """
    folhas: list[dict] = []

    # 1. Tenta o novo portal do empregador
    for url in [
        "https://empregador.esocial.gov.br/",
        "https://login.esocial.gov.br/login.aspx",
    ]:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_ESOCIAL_TIMEOUT)
            await page.wait_for_timeout(4000)

            # Se encontrar tela de login, clica em "Certificado Digital"
            for sel in [
                "a:has-text('Certificado Digital')",
                "button:has-text('Certificado')",
                "input[value*='ertificado']",
                ".certificado",
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

            # Navega até seção de totalizadores / relatórios de folha
            nav_paths = [
                ["a:has-text('Consultas')", "a:has-text('Totalizador')"],
                ["a:has-text('Relatório')", "a:has-text('Folha')"],
                ["a:has-text('Folha de Pagamento')"],
                ["a:has-text('Totalizadores')"],
                ["a:has-text('S-5011')"],
                ["a:has-text('Remuneração')"],
            ]
            for path in nav_paths:
                try:
                    for step_sel in path:
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

            if folhas:
                break

            # Tenta selecionar o ano se houver dropdown
            try:
                sel_ano = page.locator(f"select option[value='{ano}']").first
                if await sel_ano.count() > 0:
                    parent = page.locator(f"select:has(option[value='{ano}'])").first
                    await parent.select_option(str(ano))
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    folhas = _parse_esocial_lxml(html, ano)
                    if folhas:
                        break
            except Exception:
                pass

        except Exception:
            continue

    # 2. Tenta a API REST do eSocial (se autenticado, pode funcionar)
    if not folhas:
        try:
            folhas = await _tentar_api_esocial(page, context, cnpj, ano)
        except Exception:
            pass

    aviso = (
        f"Dados de folha extraídos: {len(folhas)} competência(s) de {ano}."
        if folhas
        else (
            "Acesso ao eSocial estabelecido. Os totalizadores (S-5011) dependem do "
            "fechamento mensal (S-1299) pelo empregador. Se os períodos ainda não foram "
            "fechados, os dados não estarão disponíveis. Lance manualmente ou acesse "
            "o portal do eSocial diretamente."
        )
    )
    return {"folhas": folhas, "aviso": aviso}


async def _tentar_api_esocial(page, context, cnpj: str, ano: int) -> list:
    """Tenta buscar totalizadores via API REST do eSocial (se autenticado via cookie)."""
    folhas = []
    cnpj_limpo = re.sub(r"\D", "", cnpj)
    for mes in range(1, 13):
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
    """CND Federal — Receita Federal + PGFN. Usa Playwright (portal JS-rendered)."""
    if not await _playwright_ok():
        return {"status": "em_analise", "observacao": "Playwright não disponível. Acesse: https://solucoes.receita.fazenda.gov.br/servicos/certidaointernet/pj/emitir"}

    cnpj_digits = re.sub(r'\D', '', cnpj)

    async def _tarefa(page):
        url = "https://solucoes.receita.fazenda.gov.br/Servicos/CertidaoInternet/PJ/emitir"
        await page.goto(url, wait_until="networkidle", timeout=40_000)
        # preenche CNPJ — a RF usa campo 'NrCnpj' ou 'cnpj'
        for sel in ['input[name="NrCnpj"]', 'input[name="cnpj"]', 'input[id*="cnpj" i]']:
            try:
                await page.fill(sel, cnpj_digits, timeout=3_000)
                break
            except Exception:
                pass
        # clica no botão de emissão
        for sel in ['input[type="image"]', 'button[type="submit"]', 'input[type="submit"]', 'a:has-text("Emitir")']:
            try:
                await page.click(sel, timeout=3_000)
                break
            except Exception:
                pass
        await page.wait_for_load_state("networkidle", timeout=40_000)
        return _parse_certidao_html(await page.content(), "cnd_federal", page_url=page.url)

    try:
        return await _run_playwright_no_cert(_tarefa)
    except Exception as e:
        return {"status": "em_analise", "observacao": f"Erro na consulta automática: {e}. Acesse: https://solucoes.receita.fazenda.gov.br/servicos/certidaointernet/pj/emitir"}


async def _consultar_cnd_fgts(cnpj: str) -> dict:
    """CRF FGTS — Caixa Econômica Federal. Usa Playwright (portal JSF)."""
    if not await _playwright_ok():
        return {"status": "em_analise", "observacao": "Playwright não disponível. Acesse: https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf"}

    cnpj_digits = re.sub(r'\D', '', cnpj)

    async def _tarefa(page):
        url = "https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf"
        await page.goto(url, wait_until="networkidle", timeout=40_000)
        for sel in ['input[id*="cnpj" i]', 'input[name*="cnpj" i]', 'input[type="text"]']:
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
        return _parse_certidao_html(await page.content(), "cnd_fgts", page_url=page.url)

    try:
        return await _run_playwright_no_cert(_tarefa)
    except Exception as e:
        return {"status": "em_analise", "observacao": f"Erro na consulta: {e}. Acesse: https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf"}


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
        for sel in ['button[type="submit"]', 'input[type="submit"]', 'input[type="image"]', 'a:has-text("Consultar")', 'button:has-text("Emitir")']:
            try:
                await page.click(sel, timeout=3_000)
                break
            except Exception:
                pass
        await page.wait_for_load_state("networkidle", timeout=40_000)
        return _parse_certidao_html(await page.content(), "cndt_tst", page_url=page.url)

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
        "AM": "https://certidao.trt11.jus.br/certidao/",
        "RR": "https://certidao.trt11.jus.br/certidao/",
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
        return _parse_certidao_html(await page.content(), tipo, page_url=page.url)

    try:
        return await _run_playwright_no_cert(_tarefa)
    except Exception as e:
        return {"status": "em_analise", "observacao": f"Erro na consulta SEFAZ-{uf}: {e}. Acesse: {url}"}


async def _consultar_cnd_municipal(cnpj: str, municipio: str, uf: str) -> dict:
    """CND Municipal — varia por município. Usa Playwright para municípios com portal JS."""
    _PORTAIS = {
        "manaus": "https://sefin.manaus.am.gov.br/certidao",
        "são paulo": "https://nfe.prefeitura.sp.gov.br/contribuinte/certidao.aspx",
        "rio de janeiro": "https://mobuss.rio.rj.gov.br/certidao",
        "belo horizonte": "https://bhiss.pbh.gov.br/bhiss/certidao",
        "fortaleza": "https://sefin.fortaleza.ce.gov.br/certidao",
        "curitiba": "https://www.curitiba.pr.gov.br/servicos/certidao",
        "porto alegre": "https://prefeitura.poa.br/smf/certidao",
    }
    mun_key = municipio.lower().strip()
    url = _PORTAIS.get(mun_key, "")
    if not url:
        return {
            "status": "em_analise",
            "observacao": (
                f"Portal da Prefeitura de {municipio}/{uf} ainda não integrado. "
                f"Consulte manualmente no site da prefeitura."
            ),
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
        return _parse_certidao_html(await page.content(), "cnd_municipal", page_url=page.url)

    try:
        return await _run_playwright_no_cert(_tarefa)
    except Exception as e:
        return {"status": "em_analise", "observacao": f"Erro na consulta prefeitura {municipio}: {e}. Acesse o portal da prefeitura."}


async def _consultar_cnd_falencia(cnpj: str, uf: str = "AM") -> dict:
    """Certidão de Falência/Recuperação Judicial — emitida pelos Tribunais de Justiça estaduais."""
    import httpx

    _TJ_URLS = {
        "AM": "https://www.tjam.jus.br/index.php?option=com_content&view=article&id=3038&Itemid=392",
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
    uf_upper = uf.upper()
    url = _TJ_URLS.get(uf_upper, "")

    if not url:
        return {
            "status": "em_analise",
            "observacao": (
                f"A certidão de falência é emitida pelo TJ{uf_upper} (Tribunal de Justiça). "
                f"Consulte manualmente em www.tj{uf.lower()}.jus.br"
            ),
        }

    headers = {"User-Agent": "Mozilla/5.0 Chrome/125.0.0.0"}
    async with httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True) as c:
        try:
            r = await c.get(url, headers=headers)
            html = r.text
            # Tenta POST com CNPJ
            r2 = await c.post(url, data={"cnpj": cnpj, "numeroCNPJ": cnpj}, headers=headers)
            html = r2.text
        except Exception:
            html = ""

    result = _parse_certidao_html(html, "cnd_falencia")
    if result.get("status") == "pendente":
        result["observacao"] = (
            f"TJ{uf_upper} consultado. Se não houver dados automáticos, acesse: {url}"
        )
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
    candidatas: list[date] = []

    # Padrões prioritários (contexto de validade)
    for pat in [
        r'v[aá]lid[ao]\s+(?:at[eé]|até)\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'validade\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'expira\s+em\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'prazo\s+de\s+validade\s*:?\s*(\d{2}/\d{2}/\d{4})',
        r'data\s+de\s+validade\s*:?\s*(\d{2}/\d{2}/\d{4})',
    ]:
        for m in re.finditer(pat, html, re.IGNORECASE):
            try:
                candidatas.append(datetime.strptime(m.group(1), "%d/%m/%Y").date())
            except ValueError:
                pass

    # Prioriza datas futuras; se não houver, usa a mais recente
    futuras = [d for d in candidatas if d > hoje]
    if futuras:
        data_validade = min(futuras)
    elif candidatas:
        data_validade = max(candidatas)

    # Extrai número da certidão — exclui caminhos de arquivo, hashes e referências JS
    _EXTENSOES = ('.js', '.css', '.html', '.htm', '.php', '.asp', '.json', '.png', '.jpg')
    numero = None
    for pat in [
        r'n[uú]mero\s*(?:da\s+certid[aã]o)?\s*:?\s*([A-Z0-9][A-Z0-9\-/.]{4,39})',
        r'certid[aã]o\s+n[oº°.]\s*:?\s*([A-Z0-9][A-Z0-9\-/.]{4,39})',
        r'c[oó]digo\s+(?:de\s+controle|de\s+verifica[cç][aã]o)\s*:?\s*([A-Z0-9][A-Z0-9\-]{5,29})',
        r'protocolo\s*:?\s*([A-Z0-9][A-Z0-9\-]{5,29})',
    ]:
        for m in re.finditer(pat, html, re.IGNORECASE):
            n = m.group(1).strip().rstrip('.')
            # Deve conter pelo menos um dígito para ser um número de certidão real
            if not re.search(r'\d', n):
                continue
            if not any(n.lower().endswith(ext) for ext in _EXTENSOES):
                if not re.search(r'[a-f0-9]{8}-[a-f0-9]{4}', n.lower()):  # não é UUID
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
