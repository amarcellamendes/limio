"""
Integrações com Receita Federal (PGDAS-D) e eSocial via certificado A1.

Estratégia:
  - PGDAS-D: Playwright (Chromium headless) com client certificate — o portal usa JS
  - eSocial: Playwright navegando no portal web.esocial.gov.br com client certificate

O Chromium usa o certificado A1 diretamente (PEM) para autenticar nos portais,
exatamente como o contador faria no browser, sem precisar de webservice especializado.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import tempfile, os, re

from ..database import get_db
from ..models import Cliente, Escritorio
from ..auth import get_escritorio_atual
from ..config import settings

router = APIRouter(prefix="/api/integracoes", tags=["Integrações RF/eSocial"])


@router.post("/buscar-lote")
async def buscar_lote(
    payload: dict,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """
    Busca PGDAS-D e eSocial em lote para todos os clientes com certificado configurado.
    Payload: { "ano": 2025, "cliente_ids": [1,2,3] (opcional — omitir para todos) }
    Retorna resultados parciais conforme vai completando.
    """
    from ..models import ReceitaHistorica, FolhaMensal
    from sqlalchemy import and_

    ano = int(payload.get("ano", 2025))
    cliente_ids_filtro = payload.get("cliente_ids")

    q = select(Cliente).where(
        Cliente.escritorio_id == escritorio.id,
        Cliente.ativo == True,
    )
    if cliente_ids_filtro:
        q = q.where(Cliente.id.in_(cliente_ids_filtro))

    result = await db.execute(q)
    clientes = result.scalars().all()

    resultados = []
    for cliente in clientes:
        item = {"cliente_id": cliente.id, "razao_social": cliente.razao_social, "pgdas": None, "esocial": None, "erro": None}
        try:
            cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfse")
            if erro:
                cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfe")
            if erro:
                item["erro"] = f"Certificado: {erro}"
                resultados.append(item)
                continue

            if await _playwright_disponivel():
                try:
                    res_pgdas = await _run_playwright(
                        cert_pem, key_pem,
                        "https://www8.receita.fazenda.gov.br",
                        lambda page, ctx: _tarefa_pgdas(page, ctx, cliente.cnpj, ano),
                    )
                    for rec in res_pgdas.get("receitas", []):
                        from ..routers.apuracao_router import _upsert_receita_historica
                        try:
                            existing = await db.execute(
                                select(ReceitaHistorica).where(
                                    ReceitaHistorica.cliente_id == cliente.id,
                                    ReceitaHistorica.escritorio_id == escritorio.id,
                                    ReceitaHistorica.competencia == rec["competencia"],
                                )
                            )
                            ex = existing.scalar_one_or_none()
                            if ex:
                                ex.valor_receita = rec["valor_receita"]
                                ex.origem = "pgdas_d"
                            else:
                                db.add(ReceitaHistorica(
                                    escritorio_id=escritorio.id,
                                    cliente_id=cliente.id,
                                    competencia=rec["competencia"],
                                    valor_receita=rec["valor_receita"],
                                    origem="pgdas_d",
                                ))
                        except Exception:
                            pass
                    await db.commit()
                    item["pgdas"] = {"competencias": len(res_pgdas.get("receitas", [])), "aviso": res_pgdas.get("aviso")}
                except Exception as e:
                    item["pgdas"] = {"erro": str(e)}

                try:
                    res_esocial = await _run_playwright(
                        cert_pem, key_pem,
                        "https://login.esocial.gov.br",
                        lambda page, ctx: _tarefa_esocial(page, ctx, cliente.cnpj, ano),
                    )
                    for f in res_esocial.get("folhas", []):
                        ex_r = await db.execute(
                            select(FolhaMensal).where(
                                FolhaMensal.cliente_id == cliente.id,
                                FolhaMensal.escritorio_id == escritorio.id,
                                FolhaMensal.competencia == f["competencia"],
                            )
                        )
                        ex = ex_r.scalar_one_or_none()
                        if ex:
                            ex.valor_salarios = f["valor_total_folha"]
                            ex.origem = "esocial"
                        else:
                            db.add(FolhaMensal(
                                escritorio_id=escritorio.id,
                                cliente_id=cliente.id,
                                competencia=f["competencia"],
                                valor_salarios=f["valor_total_folha"],
                                valor_total=f["valor_total_folha"],
                                origem="esocial",
                            ))
                    await db.commit()
                    item["esocial"] = {"competencias": len(res_esocial.get("folhas", [])), "aviso": res_esocial.get("aviso")}
                except Exception as e:
                    item["esocial"] = {"erro": str(e)}
        except Exception as e:
            item["erro"] = str(e)
        resultados.append(item)

    total_pgdas = sum(1 for r in resultados if r.get("pgdas") and not r["pgdas"].get("erro"))
    total_esocial = sum(1 for r in resultados if r.get("esocial") and not r["esocial"].get("erro"))
    return {
        "ano": ano,
        "total_clientes": len(resultados),
        "sucesso_pgdas": total_pgdas,
        "sucesso_esocial": total_esocial,
        "resultados": resultados,
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _carregar_certificado(cliente: Cliente, tipo: str, senha_override: str = ""):
    """Extrai PEM (cert + key) do .pfx do cliente. Retorna (cert_pem, key_pem, erro_str)."""
    path = getattr(cliente, f"{tipo}_certificado_path", None)
    senha = senha_override or getattr(cliente, f"{tipo}_certificado_senha", None) or ""

    if not path:
        return None, None, f"Certificado {tipo.upper()} não cadastrado. Faça o upload no cadastro do cliente."
    if not os.path.exists(path):
        return None, None, (
            f"Arquivo do certificado {tipo.upper()} não encontrado no servidor ({path}). "
            "Faça o upload novamente no cadastro do cliente."
        )

    try:
        from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

        with open(path, "rb") as f:
            pfx_bytes = f.read()

        p12 = load_pkcs12(pfx_bytes, senha.encode("utf-8") if senha else b"")
        cert_pem = p12.cert.certificate.public_bytes(Encoding.PEM)
        key_pem = p12.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        return cert_pem, key_pem, None
    except Exception as e:
        msg = str(e).lower()
        if "mac" in msg or "password" in msg or "invalid" in msg or "decrypt" in msg:
            return None, None, (
                f"Senha incorreta para o certificado {tipo.upper()}. "
                "Corrija a senha no cadastro do cliente e tente novamente."
            )
        return None, None, f"Erro ao abrir certificado {tipo.upper()}: {str(e)}"


async def _playwright_disponivel() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa
        return True
    except ImportError:
        return False


async def _run_playwright(cert_pem: bytes, key_pem: bytes, origem: str, tarefa_fn) -> dict:
    """
    Executa uma função de automação no Playwright com o certificado A1 configurado
    para a origem informada (ex: 'https://www8.receita.fazenda.gov.br').

    tarefa_fn recebe (page, context) e retorna o resultado.
    """
    from playwright.async_api import async_playwright

    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as cf:
        cf.write(cert_pem); cert_path = cf.name
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="wb") as kf:
        kf.write(key_pem); key_path = kf.name

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                client_certificates=[{
                    "origin": origem,
                    "certPath": cert_path,
                    "keyPath": key_path,
                }],
                ignore_https_errors=True,  # ICP-Brasil CA não está no bundle padrão
            )
            page = await context.new_page()
            try:
                resultado = await tarefa_fn(page, context)
            finally:
                await context.close()
                await browser.close()
            return resultado
    finally:
        try: os.unlink(cert_path)
        except: pass
        try: os.unlink(key_path)
        except: pass


# ─── Tarefa PGDAS-D ───────────────────────────────────────────────────────────

async def _tarefa_pgdas(page, context, cnpj: str, ano: int) -> dict:
    """
    Navega no portal do Simples Nacional e extrai receita bruta por competência.
    """
    cnpj_limpo = re.sub(r"\D", "", cnpj)

    # 1. Acessa o portal de consulta do PGDAS-D
    await page.goto(
        "https://www8.receita.fazenda.gov.br/SimplesNacional/Aplicacoes/ATBHE/pgdas.app/emPGDAS/",
        wait_until="networkidle",
        timeout=45000,
    )

    # 2. Se o portal pedir seleção de CNPJ (quando o cert tem múltiplos), seleciona o certo
    try:
        await page.wait_for_selector(f"text={cnpj_limpo[:8]}", timeout=5000)
        cnpj_link = page.locator(f"[href*='{cnpj_limpo[:8]}'], [data-cnpj*='{cnpj_limpo[:8]}']").first
        if await cnpj_link.count() > 0:
            await cnpj_link.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # Provavelmente já está no contexto do CNPJ correto

    # 3. Captura conteúdo para extração
    html = await page.content()
    receitas = _parse_pgdas_html(html, ano)

    # 4. Se não achou na página inicial, tenta navegar para "Consulta de Competências Anteriores"
    if not receitas:
        try:
            # Procura link de consulta/histórico
            for selector in ["a:has-text('Consulta')", "a:has-text('Histórico')", "a:has-text('Anterior')", "a:has-text('Extrato')"]:
                links = page.locator(selector)
                if await links.count() > 0:
                    await links.first.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    html = await page.content()
                    receitas = _parse_pgdas_html(html, ano)
                    if receitas:
                        break
        except Exception:
            pass

    return {
        "receitas": receitas,
        "aviso": (
            "Dados extraídos com sucesso do PGDAS-D!" if receitas
            else (
                "Acesso ao PGDAS-D estabelecido, mas não foram encontrados dados "
                f"de {ano} na página atual. O portal pode requerer navegação adicional."
            )
        ),
    }


# ─── Tarefa eSocial ───────────────────────────────────────────────────────────

async def _tarefa_esocial(page, context, cnpj: str, ano: int) -> dict:
    """
    Navega no portal do eSocial e extrai totais de folha de pagamento.
    """
    cnpj_limpo = re.sub(r"\D", "", cnpj)

    # 1. Acessa portal eSocial
    await page.goto(
        "https://login.esocial.gov.br/login.aspx",
        wait_until="networkidle",
        timeout=45000,
    )

    # 2. Seleciona "Certificado Digital" como método de login se houver botão
    try:
        cert_btn = page.locator("a:has-text('Certificado Digital'), button:has-text('Certificado'), input[value*='Certificado']").first
        if await cert_btn.count() > 0:
            await cert_btn.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    # 3. Aguarda login completar (redireciona para o portal após cert auth)
    await page.wait_for_load_state("networkidle", timeout=30000)

    # 4. Tenta navegar para seção de folha/remuneração
    folhas = []
    try:
        for selector in [
            "a:has-text('Folha')", "a:has-text('Remuneração')",
            "a:has-text('Relatório')", "a:has-text('Consulta')",
        ]:
            link = page.locator(selector).first
            if await link.count() > 0:
                await link.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                html = await page.content()
                folhas = _parse_esocial_html(html, ano)
                if folhas:
                    break
    except Exception:
        pass

    # 5. Se não achou via navegação, tenta extrair da página atual
    if not folhas:
        html = await page.content()
        folhas = _parse_esocial_html(html, ano)

    return {
        "folhas": folhas,
        "aviso": (
            "Dados extraídos com sucesso do eSocial!" if folhas
            else (
                "Acesso ao eSocial estabelecido. Os totalizadores de folha (S-5011) "
                "podem estar em outra seção do portal. Lance o valor manualmente ou "
                "acesse o portal do eSocial para consultar."
            )
        ),
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/{cliente_id}/buscar-pgdas")
async def buscar_pgdas(
    cliente_id: int,
    ano: int = Query(default=2025),
    senha: str = Query(default=""),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """
    Busca receita bruta no PGDAS-D usando Playwright com o certificado A1 do cliente.
    """
    result = await db.execute(
        select(Cliente).where(Cliente.id == cliente_id, Cliente.escritorio_id == escritorio.id)
    )
    cliente = result.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")

    cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfse", senha)
    if erro:
        cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfe", senha)
    if erro:
        raise HTTPException(400, erro)

    if not await _playwright_disponivel():
        raise HTTPException(503, "Módulo de automação não disponível. Contate o suporte.")

    try:
        resultado = await _run_playwright(
            cert_pem, key_pem,
            "https://www8.receita.fazenda.gov.br",
            lambda page, ctx: _tarefa_pgdas(page, ctx, cliente.cnpj, ano),
        )
        return {"status": "ok", **resultado}
    except Exception as e:
        msg = str(e)
        if "timeout" in msg.lower() or "Timeout" in msg:
            raise HTTPException(504, "Tempo esgotado ao conectar ao portal da Receita Federal. Tente novamente.")
        if "net::" in msg or "SSL" in msg:
            raise HTTPException(502, f"Erro de conexão com a Receita Federal: {msg}")
        raise HTTPException(502, f"Erro ao acessar PGDAS-D: {msg}")


@router.post("/{cliente_id}/buscar-esocial")
async def buscar_esocial(
    cliente_id: int,
    ano: int = Query(default=2025),
    senha: str = Query(default=""),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """
    Busca dados de folha no eSocial usando Playwright com o certificado A1 do cliente.
    """
    result = await db.execute(
        select(Cliente).where(Cliente.id == cliente_id, Cliente.escritorio_id == escritorio.id)
    )
    cliente = result.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")

    cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfse", senha)
    if erro:
        cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfe", senha)
    if erro:
        raise HTTPException(400, erro)

    if not await _playwright_disponivel():
        raise HTTPException(503, "Módulo de automação não disponível. Contate o suporte.")

    try:
        resultado = await _run_playwright(
            cert_pem, key_pem,
            "https://login.esocial.gov.br",
            lambda page, ctx: _tarefa_esocial(page, ctx, cliente.cnpj, ano),
        )
        return {"status": "ok", **resultado}
    except Exception as e:
        msg = str(e)
        if "timeout" in msg.lower():
            raise HTTPException(504, "Tempo esgotado ao conectar ao eSocial. Tente novamente.")
        raise HTTPException(502, f"Erro ao acessar eSocial: {msg}")


# ─── Parsers de resposta ───────────────────────────────────────────────────────

def _parse_pgdas_html(html: str, ano: int) -> list:
    """Extrai competência + receita bruta da página HTML do PGDAS-D."""
    resultados = []
    # Padrão: "01/2025" ou "2025-01" seguido de valor monetário "R$ 12.345,67"
    patterns = [
        re.compile(r'(\d{2}/\d{4})[^<]*?R\$\s*([\d.,]+)', re.DOTALL),
        re.compile(r'(\d{4}-\d{2})[^<]*?R\$\s*([\d.,]+)', re.DOTALL),
    ]
    for pattern in patterns:
        for m in pattern.finditer(html):
            comp_raw = m.group(1)
            val_str = m.group(2).replace(".", "").replace(",", ".")
            try:
                if "/" in comp_raw:
                    mes, comp_ano = comp_raw.split("/")
                else:
                    comp_ano, mes = comp_raw.split("-")
                if int(comp_ano) == ano:
                    comp = f"{comp_ano}-{mes.zfill(2)}"
                    if not any(r["competencia"] == comp for r in resultados):
                        resultados.append({
                            "competencia": comp,
                            "valor_receita": float(val_str),
                            "origem": "pgdas_d",
                        })
            except Exception:
                pass
    return resultados


def _parse_esocial_html(html: str, ano: int) -> list:
    """Extrai valores de folha da página HTML do eSocial."""
    resultados = []
    # Busca por padrões de competência + valor total
    pattern = re.compile(r'(\d{2}/\d{4}|\d{4}-\d{2})[^<]*?R\$\s*([\d.,]+)', re.DOTALL)
    for m in pattern.finditer(html):
        comp_raw = m.group(1)
        val_str = m.group(2).replace(".", "").replace(",", ".")
        try:
            if "/" in comp_raw:
                mes, comp_ano = comp_raw.split("/")
            else:
                comp_ano, mes = comp_raw.split("-")
            if int(comp_ano) == ano:
                comp = f"{comp_ano}-{mes.zfill(2)}"
                if not any(r["competencia"] == comp for r in resultados):
                    resultados.append({
                        "competencia": comp,
                        "valor_total_folha": float(val_str),
                        "origem": "esocial",
                    })
        except Exception:
            pass
    return resultados
