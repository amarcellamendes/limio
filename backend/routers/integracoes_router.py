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
        resultado = await _run_playwright(
            cert_pem, key_pem,
            "https://www8.receita.fazenda.gov.br",
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
        resultado = await _run_playwright(
            cert_pem, key_pem,
            "https://empregador.esocial.gov.br",
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
    Busca faturamento de empresa LP/LR via e-CAC (Receita Federal).
    Usa o certificado A1 para acessar 'Consulta de Receita Bruta' ou DCTF.
    """
    cliente = await _get_cliente(cliente_id, escritorio.id, db)
    cert_pem, key_pem = await _cert_ou_erro(cliente, senha)

    if not await _playwright_ok():
        raise HTTPException(503, "Módulo de automação não disponível.")

    try:
        # Registra cert para ambos os domínios (e-CAC + DCTFWeb)
        resultado = await _run_playwright_multi(
            cert_pem, key_pem,
            ["https://cav.receita.fazenda.gov.br", "https://dctfweb.receita.fazenda.gov.br"],
            lambda p, c: _tarefa_ecac_faturamento(p, c, cliente.cnpj, ano),
        )
        for rec in resultado.get("receitas", []):
            await _upsert_receita(db, escritorio.id, cliente.id, rec["competencia"], rec["valor_receita"], "ecac")
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
        return await _consultar_cnd_falencia(cnpj)
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
    debug_url = ""

    # 1. Acessa portal principal do Simples Nacional (não o emPGDAS direto)
    await page.goto(
        "https://www8.receita.fazenda.gov.br/SimplesNacional/",
        wait_until="domcontentloaded",
        timeout=_PGDAS_TIMEOUT,
    )
    await page.wait_for_timeout(3000)
    debug_url = page.url

    # 2. Se houver seleção de CNPJ (certificado multiempresa), seleciona o correto
    try:
        await page.wait_for_selector(f"text={cnpj_limpo[:8]}", timeout=4000)
        el = page.locator(f"a:has-text('{cnpj_limpo[:8]}')").first
        if await el.count() > 0:
            await el.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
    except Exception:
        pass

    # 3. Tenta navegar para a consulta de competências pelo menu
    menu_links = [
        "a:has-text('PGDAS')", "a:has-text('Consulta')", "a:has-text('Declarações')",
        "a:has-text('Extrato')", "a:has-text('Apuração')", "a:has-text('Competências')",
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

    # 6. Tenta URL alternativa de extrato/consulta
    if not receitas:
        try:
            await page.goto(
                f"https://www8.receita.fazenda.gov.br/SimplesNacional/Aplicacoes/ATBHE/pgdas.app/extrato.app/?ano={ano}",
                wait_until="domcontentloaded", timeout=30000
            )
            await page.wait_for_timeout(3000)
            html = await page.content()
            receitas = _parse_pgdas_lxml(html, ano)
        except Exception:
            pass

    aviso = (
        f"Dados extraídos: {len(receitas)} competência(s) de {ano}."
        if receitas
        else (
            f"Acesso ao PGDAS-D estabelecido (URL: {debug_url}). "
            "Nenhuma declaração de {ano} encontrada — pode ser que o ano ainda não tenha "
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
    }


# ─── Consulta automática de certidões ────────────────────────────────────────

async def _consultar_cnd_federal(cnpj: str) -> dict:
    """CND Federal — Receita Federal + PGFN. Consulta por CNPJ via httpx."""
    import httpx

    url = "https://solucoes.receita.fazenda.gov.br/servicos/certidaointernet/pj/emitir"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
        "Referer": url,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True) as c:
        # GET primeiro para obter cookies/token
        r0 = await c.get(url, headers=headers)
        # POST com o CNPJ
        r1 = await c.post(url, data={"cnpj": cnpj}, headers=headers)
        html = r1.text

    return _parse_certidao_html(html, "cnd_federal")


async def _consultar_cnd_fgts(cnpj: str) -> dict:
    """CRF FGTS — Caixa Econômica Federal."""
    import httpx

    cnpj_fmt = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
    url = f"https://consulta.caixa.gov.br/servicos/fgts-certidao/{cnpj}"
    headers = {"User-Agent": "Mozilla/5.0 Chrome/125.0.0.0"}
    async with httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True) as c:
        r = await c.get(url, headers=headers)
        html = r.text
        # Tenta também POST
        if "REGULAR" not in html.upper() and "IRREGULAR" not in html.upper():
            r2 = await c.post(
                "https://consulta.caixa.gov.br/servicos/fgts-certidao/",
                data={"cnpj": cnpj, "empresa": cnpj_fmt},
                headers=headers,
            )
            html = r2.text

    return _parse_certidao_html(html, "cnd_fgts")


async def _consultar_cndt_tst(cnpj: str) -> dict:
    """CNDT — TST Nacional. Formulário público por CNPJ."""
    import httpx

    url = "https://www.tst.jus.br/certidao"
    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/125.0.0.0",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True) as c:
        r0 = await c.get(url, headers=headers)
        html0 = r0.text
        # Extrai action/token se houver
        action_match = re.search(r'action=["\']([^"\']+)["\']', html0)
        action = action_match.group(1) if action_match else url
        if not action.startswith("http"):
            action = "https://www.tst.jus.br" + action
        # POST
        r1 = await c.post(action, data={"cpf_cnpj": cnpj, "nrCpfCnpj": cnpj}, headers=headers)
        html = r1.text

    return _parse_certidao_html(html, "cndt_tst")


async def _consultar_cndt_trt(cnpj: str, uf: str) -> dict:
    """CNDT TRT Regional — URL varia por TRT/UF."""
    import httpx

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

    headers = {"User-Agent": "Mozilla/5.0 Chrome/125.0.0.0"}
    async with httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True) as c:
        try:
            r0 = await c.get(trt_url, headers=headers)
            action = re.search(r'action=["\']([^"\']+)["\']', r0.text)
            act = action.group(1) if action else trt_url
            if not act.startswith("http"):
                act = trt_url.rstrip("/") + "/" + act.lstrip("/")
            r1 = await c.post(act, data={"nrCpfCnpj": cnpj, "cpf_cnpj": cnpj, "cnpj": cnpj}, headers=headers)
            html = r1.text
        except Exception:
            html = ""

    result = _parse_certidao_html(html, "cndt_trt")
    result["observacao"] = f"TRT consultado: {uf} → {trt_url}"
    return result


async def _consultar_cnd_estadual(cnpj: str, uf: str, tipo: str) -> dict:
    """CND Estadual — varia por UF/SEFAZ. Estratégia: GET no portal + extração."""
    import httpx

    _SEFAZ_CNDS = {
        "AM": "https://www.sefaz.am.gov.br/portal/certidao-negativa",
        "SP": "https://www10.fazenda.sp.gov.br/CertidaoNegativaDeb/Pages/EmissaoCertidao.aspx",
        "RJ": "https://www4.fazenda.rj.gov.br/consultaDivida/pages/certidaoNegativa.jsf",
        "MG": "https://www.fazenda.mg.gov.br/empresas/impostos_estaduais/certidao/",
    }
    url = _SEFAZ_CNDS.get(uf.upper(), "")
    if not url:
        return {
            "status": "em_analise",
            "observacao": f"Portal SEFAZ-{uf} ainda não integrado. Consulte manualmente em www.sefaz.{uf.lower()}.gov.br",
        }

    headers = {"User-Agent": "Mozilla/5.0 Chrome/125.0.0.0"}
    async with httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True) as c:
        try:
            r0 = await c.get(url, headers=headers)
            html = r0.text
            r1 = await c.post(url, data={"cnpj": cnpj, "nrCnpj": cnpj}, headers=headers)
            html = r1.text
        except Exception:
            html = ""

    return _parse_certidao_html(html, tipo)


async def _consultar_cnd_municipal(cnpj: str, municipio: str, uf: str) -> dict:
    """CND Municipal — varia por município. Estratégia limitada a municípios conhecidos."""
    _PORTAIS = {
        "manaus": "https://sefin.manaus.am.gov.br/certidao",
        "são paulo": "https://nfe.prefeitura.sp.gov.br/contribuinte/certidao.aspx",
        "rio de janeiro": "https://mobuss.rio.rj.gov.br/certidao",
        "belo horizonte": "https://bhiss.pbh.gov.br/bhiss/certidao",
    }
    mun_key = municipio.lower().strip()
    url = _PORTAIS.get(mun_key, "")
    if not url:
        return {
            "status": "em_analise",
            "observacao": f"Portal da Prefeitura de {municipio}/{uf} ainda não integrado. Consulte manualmente.",
        }

    import httpx
    headers = {"User-Agent": "Mozilla/5.0 Chrome/125.0.0.0"}
    async with httpx.AsyncClient(verify=False, timeout=30, follow_redirects=True) as c:
        try:
            r = await c.get(url, params={"cnpj": cnpj}, headers=headers)
            html = r.text
        except Exception:
            html = ""

    return _parse_certidao_html(html, "cnd_municipal")


async def _consultar_cnd_falencia(cnpj: str) -> dict:
    """Certidão de Falência — consulta pública nas Juntas Comerciais."""
    return {
        "status": "em_analise",
        "observacao": (
            "A certidão de falência é emitida pela Junta Comercial de cada estado. "
            "Não há endpoint público unificado. Consulte manualmente: "
            "https://www.governodigital.gov.br/junta-comercial"
        ),
    }


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
            for row in rows:
                cells = [" ".join(td.text_content().split()) for td in row.iter("td", "th")]
                row_text = " ".join(cells)

                # Procura competência MM/AAAA
                comp_m = re.search(r'(\d{2})/(\d{4})', row_text)
                if not comp_m:
                    continue
                mes_str, ano_str = comp_m.group(1), comp_m.group(2)
                if int(ano_str) != ano:
                    continue
                if mes_fixo and int(mes_str) != mes_fixo:
                    continue

                # Procura valor R$
                val_m = re.search(r'R\$\s*([\d.,]+)', row_text)
                if not val_m:
                    # Tenta achar número grande na linha (receita bruta esperada)
                    val_m = re.search(r'([\d]{1,3}(?:\.[\d]{3})+,[\d]{2})', row_text)
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
            for row in table.iter("tr"):
                cells = [" ".join(td.text_content().split()) for td in row.iter("td")]
                row_text = " ".join(cells)
                comp_m = re.search(r'(\d{2})/(\d{4})', row_text)
                if not comp_m:
                    continue
                mes_s, ano_s = comp_m.group(1), comp_m.group(2)
                if int(ano_s) != ano:
                    continue
                val_m = re.search(r'R\$\s*([\d.,]+)', row_text)
                if not val_m:
                    val_m = re.search(r'([\d]{1,3}(?:\.[\d]{3})+,[\d]{2})', row_text)
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


def _parse_certidao_html(html: str, tipo: str) -> dict:
    """Extrai status e validade de uma certidão a partir do HTML retornado pelo portal."""
    html_up = html.upper()

    # Determina status
    if any(k in html_up for k in ["NEGATIVA COM EFEITO DE POSITIVA", "POSITIVA COM EFEITO DE NEGATIVA"]):
        status = "regular"  # juridicamente tem efeito de negativa
    elif any(k in html_up for k in ["NEGATIVA", "REGULAR", "REGULARIDADE"]):
        status = "regular"
    elif any(k in html_up for k in ["POSITIVA", "DÉBITOS", "IRREGUL", "PENDÊNCIA", "DEVEDOR"]):
        status = "irregular"
    elif any(k in html_up for k in ["PROCESSANDO", "AGUARDANDO", "ANÁLISE"]):
        status = "em_analise"
    else:
        status = "pendente"

    # Extrai data de validade
    data_validade = None
    for pat in [
        r'v[aá]lid[ao]\s+at[eé]\s+(\d{2}/\d{2}/\d{4})',
        r'validade[:\s]+(\d{2}/\d{2}/\d{4})',
        r'expira[:\s]+(\d{2}/\d{2}/\d{4})',
        r'(\d{2}/\d{2}/\d{4})',  # último recurso: primeira data no documento
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            try:
                data_validade = datetime.strptime(m.group(1), "%d/%m/%Y").date()
                break
            except ValueError:
                pass

    # Extrai número da certidão
    numero = None
    for pat in [
        r'n[uú]mero\s*:?\s*([A-Z0-9\-/.]{5,30})',
        r'n[oº°]\s*:?\s*([A-Z0-9\-/.]{5,30})',
        r'certid[aã]o\s+n[oº°]?\s*:?\s*([A-Z0-9\-/.]{5,30})',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            numero = m.group(1).strip()
            break

    return {
        "status": status,
        "data_validade": data_validade,
        "numero_certidao": numero,
        "observacao": None,
    }
