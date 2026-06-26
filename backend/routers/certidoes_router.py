"""
Router: Certidões (Brazilian company certificates) CRUD API
Handles: CND Federal, CND Falência, CND FGTS, CNDT TST, CNDT TRT,
         CND Estadual, CND Estadual NC, CND Municipal
"""

import io
import os
import zipfile
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_escritorio_atual
from ..config import settings
from ..database import get_db
from ..models import Certidao, Cliente, Escritorio

router = APIRouter(prefix="/api/certidoes", tags=["certidões"])


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

TIPOS_VALIDOS = {
    "cnd_federal",
    "cnd_falencia",
    "cnd_fgts",
    "cndt_tst",
    "cndt_trt",
    "cnd_estadual",
    "cnd_estadual_nc",
    "cnd_municipal",
}

STATUS_VALIDOS = {"regular", "irregular", "pendente", "em_analise", "vencida"}

TRT_POR_UF: dict[str, tuple[str, str]] = {
    "RJ": ("TRT 1 — Rio de Janeiro", "https://certidao.trt1.jus.br/"),
    "SP": ("TRT 2 — São Paulo (Capital)", "https://www.trt2.jus.br/"),
    "MG": ("TRT 3 — Minas Gerais", "https://certidao.trt3.jus.br/"),
    "RS": ("TRT 4 — Rio Grande do Sul", "https://www.trt4.jus.br/"),
    "BA": ("TRT 5 — Bahia", "https://www.trt5.jus.br/"),
    "PE": ("TRT 6 — Pernambuco", "https://www.trt6.jus.br/"),
    "CE": ("TRT 7 — Ceará", "https://www.trt7.jus.br/"),
    "PA": ("TRT 8 — Pará e Amapá", "https://www.trt8.jus.br/"),
    "AP": ("TRT 8 — Pará e Amapá", "https://www.trt8.jus.br/"),
    "PR": ("TRT 9 — Paraná", "https://www.trt9.jus.br/"),
    "DF": ("TRT 10 — Distrito Federal e Tocantins", "https://www.trt10.jus.br/"),
    "TO": ("TRT 10 — Distrito Federal e Tocantins", "https://www.trt10.jus.br/"),
    "AM": ("TRT 11 — Amazonas e Roraima", "https://www.trt11.jus.br/"),
    "RR": ("TRT 11 — Amazonas e Roraima", "https://www.trt11.jus.br/"),
    "SC": ("TRT 12 — Santa Catarina", "https://www.trt12.jus.br/"),
    "PB": ("TRT 13 — Paraíba", "https://www.trt13.jus.br/"),
    "RO": ("TRT 14 — Rondônia e Acre", "https://www.trt14.jus.br/"),
    "AC": ("TRT 14 — Rondônia e Acre", "https://www.trt14.jus.br/"),
    "MA": ("TRT 16 — Maranhão", "https://www.trt16.jus.br/"),
    "ES": ("TRT 17 — Espírito Santo", "https://www.trt17.jus.br/"),
    "GO": ("TRT 18 — Goiás", "https://www.trt18.jus.br/"),
    "AL": ("TRT 19 — Alagoas", "https://www.trt19.jus.br/"),
    "SE": ("TRT 20 — Sergipe", "https://www.trt20.jus.br/"),
    "RN": ("TRT 21 — Rio Grande do Norte", "https://www.trt21.jus.br/"),
    "PI": ("TRT 22 — Piauí", "https://www.trt22.jus.br/"),
    "MT": ("TRT 23 — Mato Grosso", "https://www.trt23.jus.br/"),
    "MS": ("TRT 24 — Mato Grosso do Sul", "https://www.trt24.jus.br/"),
}

SEFAZ_POR_UF: dict[str, tuple[str, str]] = {
    "AM": ("SEFAZ-AM", "https://www.sefaz.am.gov.br/"),
    "SP": ("SEFAZ-SP", "https://www.fazenda.sp.gov.br/"),
    "RJ": ("SEFAZ-RJ", "https://www.fazenda.rj.gov.br/"),
    "MG": ("SEF-MG", "https://www.fazenda.mg.gov.br/"),
    "RS": ("SEFAZ-RS", "https://www.sefaz.rs.gov.br/"),
    "PR": ("SEFAZ-PR", "https://www.fazenda.pr.gov.br/"),
    "BA": ("SEFAZ-BA", "https://www.sefaz.ba.gov.br/"),
    "PE": ("SEFAZ-PE", "https://www.sefaz.pe.gov.br/"),
    "CE": ("SEFAZ-CE", "https://www.sefaz.ce.gov.br/"),
    "SC": ("SEF-SC", "https://www.sef.sc.gov.br/"),
    "GO": ("SEFAZ-GO", "https://www.sefaz.go.gov.br/"),
    "DF": ("SEF-DF", "https://www.economia.df.gov.br/"),
    "PA": ("SEFAZ-PA", "https://www.sefa.pa.gov.br/"),
    "MA": ("SEFAZ-MA", "https://www.sefaz.ma.gov.br/"),
    "ES": ("SEFAZ-ES", "https://internet.sefaz.es.gov.br/"),
    "MT": ("SEFAZ-MT", "https://www.sefaz.mt.gov.br/"),
    "MS": ("SEFAZ-MS", "https://www.sefaz.ms.gov.br/"),
    "PB": ("SEFAZ-PB", "https://www.sefaz.pb.gov.br/"),
    "RN": ("SET-RN", "https://www.set.rn.gov.br/"),
    "AL": ("SEFAZ-AL", "https://www.sefaz.al.gov.br/"),
    "SE": ("SEFAZ-SE", "https://www.sefaz.se.gov.br/"),
    "PI": ("SEFAZ-PI", "https://www.sefaz.pi.gov.br/"),
    "RO": ("SEFAZ-RO", "https://www.sefin.ro.gov.br/"),
    "AC": ("SEF-AC", "https://www.sefaz.ac.gov.br/"),
    "TO": ("SEFAZ-TO", "https://www.sefaz.to.gov.br/"),
    "AP": ("SEFAZ-AP", "https://www.sefaz.ap.gov.br/"),
    "RR": ("SEFAZ-RR", "https://www.sefaz.rr.gov.br/"),
}

PORTAIS_FIXOS: dict[str, Optional[str]] = {
    "cnd_federal": "https://solucoes.receita.fazenda.gov.br/servicos/certidaointernet/pj/emitir",
    "cnd_falencia": None,  # varies by state (Tribunal de Justiça)
    "cnd_fgts": "https://consulta.caixa.gov.br/servicos/fgts-certidao/",
    "cndt_tst": "https://www.tst.jus.br/certidao",
    "cndt_trt": None,       # determined by UF via TRT_POR_UF
    "cnd_estadual": None,   # determined by UF via SEFAZ_POR_UF
    "cnd_estadual_nc": None,  # determined by UF via SEFAZ_POR_UF
    "cnd_municipal": None,  # determined by municipality
}

NOMES_DESCRITIVOS_PADRAO: dict[str, str] = {
    "cnd_federal": "CND Federal — Receita Federal / PGFN",
    "cnd_falencia": "Certidão de Falência e Recuperação Judicial",
    "cnd_fgts": "Certidão de Regularidade do FGTS — CEF",
    "cndt_tst": "CNDT — Certidão Negativa de Débitos Trabalhistas (TST)",
    "cndt_trt": "Certidão do TRT Regional",
    "cnd_estadual": "CND Estadual — Regularidade Fiscal (ICMS / SEFAZ)",
    "cnd_estadual_nc": "Certidão de Não Contribuinte Estadual (ICMS)",
    "cnd_municipal": "CND Municipal — ISS / Prefeitura",
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_portal_url(tipo: str, uf: str = "") -> tuple[str, str]:
    """Return (nome_portal, url) for a given certidão type and optional UF.

    For UF-dependent types (cndt_trt, cnd_estadual, cnd_estadual_nc), the UF
    is required to resolve the correct portal.  When UF is not provided (or not
    found in the mapping), returns a generic fallback label with an empty URL.
    """
    uf = uf.upper().strip() if uf else ""

    if tipo == "cnd_falencia":
        tj_nome = f"Tribunal de Justiça — TJ{uf}" if uf else "Tribunal de Justiça (TJ — varia por estado)"
        return (tj_nome, "")

    if tipo in ("cndt_trt",):
        if uf and uf in TRT_POR_UF:
            return TRT_POR_UF[uf]
        return ("TRT Regional (UF não informada)", "")

    if tipo in ("cnd_estadual", "cnd_estadual_nc"):
        if uf and uf in SEFAZ_POR_UF:
            return SEFAZ_POR_UF[uf]
        return ("SEFAZ Estadual (UF não informada)", "")

    if tipo == "cnd_municipal":
        return ("Prefeitura Municipal", "")

    url = PORTAIS_FIXOS.get(tipo, "")
    nome = NOMES_DESCRITIVOS_PADRAO.get(tipo, tipo)
    return (nome, url or "")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class CertidaoCreate(BaseModel):
    cliente_id: int
    tipo: str
    nome_descritivo: Optional[str] = None
    data_consulta: Optional[date] = None
    data_validade: Optional[date] = None
    data_agendamento: Optional[date] = None
    status: str = "pendente"
    numero_certidao: Optional[str] = None
    arquivo_path: Optional[str] = None
    observacao: Optional[str] = None
    enviar_email: bool = False
    pasta_destino: Optional[str] = None


class CertidaoUpdate(BaseModel):
    nome_descritivo: Optional[str] = None
    data_consulta: Optional[date] = None
    data_validade: Optional[date] = None
    data_agendamento: Optional[date] = None
    status: Optional[str] = None
    numero_certidao: Optional[str] = None
    arquivo_path: Optional[str] = None
    observacao: Optional[str] = None
    enviar_email: Optional[bool] = None
    pasta_destino: Optional[str] = None


class CertidaoRead(BaseModel):
    id: int
    escritorio_id: int
    cliente_id: int
    tipo: str
    nome_descritivo: Optional[str]
    data_consulta: Optional[date]
    data_validade: Optional[date]
    data_agendamento: Optional[date]
    status: str
    numero_certidao: Optional[str]
    arquivo_path: Optional[str]
    observacao: Optional[str]
    enviar_email: bool
    pasta_destino: Optional[str]
    criado_em: datetime
    atualizado_em: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[CertidaoRead])
async def listar_certidoes(
    cliente_id: Optional[int] = Query(None, description="Filtrar por cliente"),
    tipo: Optional[str] = Query(None, description="Filtrar por tipo de certidão"),
    status: Optional[str] = Query(None, description="Filtrar por status"),
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """List all certidões for the current escritório, optionally filtered by
    cliente_id, tipo, or status."""
    stmt = select(Certidao).where(Certidao.escritorio_id == escritorio.id)

    if cliente_id is not None:
        stmt = stmt.where(Certidao.cliente_id == cliente_id)

    if tipo is not None:
        if tipo not in TIPOS_VALIDOS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Tipo inválido: '{tipo}'. Tipos válidos: {sorted(TIPOS_VALIDOS)}",
            )
        stmt = stmt.where(Certidao.tipo == tipo)

    if status is not None:
        if status not in STATUS_VALIDOS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Status inválido: '{status}'. Status válidos: {sorted(STATUS_VALIDOS)}",
            )
        stmt = stmt.where(Certidao.status == status)

    stmt = stmt.order_by(Certidao.cliente_id, Certidao.tipo)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=CertidaoRead, status_code=status.HTTP_201_CREATED)
async def criar_certidao(
    payload: CertidaoCreate,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """Register a new certidão for a client."""
    if payload.tipo not in TIPOS_VALIDOS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Tipo inválido: '{payload.tipo}'. Tipos válidos: {sorted(TIPOS_VALIDOS)}",
        )

    if payload.status not in STATUS_VALIDOS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status inválido: '{payload.status}'. Status válidos: {sorted(STATUS_VALIDOS)}",
        )

    # Verify the client belongs to this escritório
    cliente_result = await db.execute(
        select(Cliente).where(
            Cliente.id == payload.cliente_id,
            Cliente.escritorio_id == escritorio.id,
        )
    )
    cliente = cliente_result.scalar_one_or_none()
    if cliente is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cliente {payload.cliente_id} não encontrado neste escritório.",
        )

    nome_descritivo = payload.nome_descritivo or NOMES_DESCRITIVOS_PADRAO.get(
        payload.tipo, payload.tipo
    )

    certidao = Certidao(
        escritorio_id=escritorio.id,
        cliente_id=payload.cliente_id,
        tipo=payload.tipo,
        nome_descritivo=nome_descritivo,
        data_consulta=payload.data_consulta,
        data_validade=payload.data_validade,
        data_agendamento=payload.data_agendamento,
        status=payload.status,
        numero_certidao=payload.numero_certidao,
        arquivo_path=payload.arquivo_path,
        observacao=payload.observacao,
        enviar_email=payload.enviar_email,
        pasta_destino=payload.pasta_destino,
    )

    db.add(certidao)
    await db.commit()
    await db.refresh(certidao)
    return certidao


@router.put("/{certidao_id}", response_model=CertidaoRead)
async def atualizar_certidao(
    certidao_id: int,
    payload: CertidaoUpdate,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """Update an existing certidão (status, validade, número, arquivo_path,
    agendamento, etc.)."""
    result = await db.execute(
        select(Certidao).where(
            Certidao.id == certidao_id,
            Certidao.escritorio_id == escritorio.id,
        )
    )
    certidao = result.scalar_one_or_none()
    if certidao is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Certidão {certidao_id} não encontrada.",
        )

    if payload.status is not None and payload.status not in STATUS_VALIDOS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status inválido: '{payload.status}'. Status válidos: {sorted(STATUS_VALIDOS)}",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(certidao, field, value)

    certidao.atualizado_em = datetime.utcnow()

    await db.commit()
    await db.refresh(certidao)
    return certidao


@router.delete("/{certidao_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deletar_certidao(
    certidao_id: int,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """Delete a certidão permanently."""
    result = await db.execute(
        select(Certidao).where(
            Certidao.id == certidao_id,
            Certidao.escritorio_id == escritorio.id,
        )
    )
    certidao = result.scalar_one_or_none()
    if certidao is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Certidão {certidao_id} não encontrada.",
        )

    await db.delete(certidao)
    await db.commit()


@router.get("/tipos-info")
async def tipos_info(
    uf: Optional[str] = Query(
        None,
        description=(
            "UF da empresa (e.g. 'AM'). Necessária para tipos que dependem de "
            "UF: cndt_trt, cnd_estadual, cnd_estadual_nc."
        ),
        min_length=2,
        max_length=2,
    ),
):
    """Return metadata about all certidão types, including portal URLs.

    For UF-dependent types (cndt_trt, cnd_estadual, cnd_estadual_nc), pass
    ``?uf=AM`` (or any two-letter state code) to get the correct portal URL
    and portal name.
    """
    tipos_info_list = []
    for tipo in sorted(TIPOS_VALIDOS):
        nome_portal, url_portal = _get_portal_url(tipo, uf or "")
        entry: dict = {
            "tipo": tipo,
            "nome_descritivo_padrao": NOMES_DESCRITIVOS_PADRAO.get(tipo, tipo),
            "nome_portal": nome_portal,
            "url_portal": url_portal or None,
            "depende_uf": tipo in ("cndt_trt", "cnd_estadual", "cnd_estadual_nc", "cnd_municipal"),
        }

        # For TRT, include the full mapping so the front-end can render a
        # dropdown without an extra request.
        if tipo == "cndt_trt":
            entry["trt_por_uf"] = {
                uf_key: {"nome": nome, "url": url}
                for uf_key, (nome, url) in TRT_POR_UF.items()
            }

        # For SEFAZ types, include the full mapping as well.
        if tipo in ("cnd_estadual", "cnd_estadual_nc"):
            entry["sefaz_por_uf"] = {
                uf_key: {"nome": nome, "url": url}
                for uf_key, (nome, url) in SEFAZ_POR_UF.items()
            }

        tipos_info_list.append(entry)

    return {
        "uf_consultada": uf.upper() if uf else None,
        "tipos": tipos_info_list,
    }


@router.get("/vencendo", response_model=list[CertidaoRead])
async def certidoes_vencendo(
    dias: int = Query(
        30,
        ge=1,
        le=365,
        description="Número de dias à frente para buscar certidões vencendo.",
    ),
    cliente_id: Optional[int] = Query(None, description="Filtrar por cliente"),
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """List certidões expiring within the next N days.

    Useful for dashboard alerts and proactive renewal management.
    Returns certidões whose ``data_validade`` falls between today and
    today + N days (inclusive), ordered by closest expiry date first.
    """
    hoje = date.today()
    limite = hoje + timedelta(days=dias)

    stmt = (
        select(Certidao)
        .where(
            Certidao.escritorio_id == escritorio.id,
            Certidao.data_validade.is_not(None),
            Certidao.data_validade >= hoje,
            Certidao.data_validade <= limite,
        )
        .order_by(Certidao.data_validade)
    )

    if cliente_id is not None:
        stmt = stmt.where(Certidao.cliente_id == cliente_id)

    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/consultar-lote")
async def consultar_lote(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """
    Consulta automática em lote: itera todas as certidões cadastradas (ou de um cliente)
    e atualiza status, validade e número via automação.

    Payload opcional: { "cliente_id": 3 } para filtrar por cliente.
    """
    from ..routers.integracoes_router import _executar_consulta_certidao

    cliente_id = payload.get("cliente_id")
    stmt = select(Certidao).where(Certidao.escritorio_id == escritorio.id)
    if cliente_id:
        stmt = stmt.where(Certidao.cliente_id == cliente_id)
    result = await db.execute(stmt)
    certidoes = result.scalars().all()

    atualizadas, erros = [], []
    for cert in certidoes:
        cli_r = await db.execute(select(Cliente).where(Cliente.id == cert.cliente_id))
        cliente = cli_r.scalar_one_or_none()
        if not cliente:
            continue
        cnpj = cliente.cnpj or ""
        uf = (cliente.uf or "AM").upper()
        try:
            res = await _executar_consulta_certidao(cert.tipo, cnpj, uf, cliente)
            if res.get("status") and res["status"] != "pendente":
                cert.status = res["status"]
            if res.get("data_validade"):
                cert.data_validade = res["data_validade"]
            if res.get("numero_certidao"):
                cert.numero_certidao = res["numero_certidao"]
            if res.get("observacao"):
                cert.observacao = res["observacao"]
            cert.data_consulta = datetime.utcnow()
            atualizadas.append({"id": cert.id, "tipo": cert.tipo, "status": cert.status})
        except Exception as e:
            erros.append({"id": cert.id, "tipo": cert.tipo, "erro": str(e)})

    await db.commit()
    return {
        "total": len(certidoes),
        "atualizadas": len(atualizadas),
        "erros": len(erros),
        "detalhe": atualizadas,
        "detalhe_erros": erros,
    }


# ---------------------------------------------------------------------------
# Download de PDFs
# ---------------------------------------------------------------------------

@router.get("/{certidao_id}/download-pdf")
async def download_pdf(
    certidao_id: int,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """Baixa o PDF salvo de uma certidão."""
    result = await db.execute(
        select(Certidao).where(
            Certidao.id == certidao_id,
            Certidao.escritorio_id == escritorio.id,
        )
    )
    cert = result.scalar_one_or_none()
    if cert is None:
        raise HTTPException(404, "Certidão não encontrada.")
    if not cert.arquivo_path or not os.path.exists(cert.arquivo_path):
        raise HTTPException(404, "PDF não disponível para esta certidão.")

    nome_arquivo = os.path.basename(cert.arquivo_path)
    return FileResponse(
        cert.arquivo_path,
        media_type="application/pdf",
        filename=nome_arquivo,
    )


class DownloadZipPayload(BaseModel):
    ids: list[int]


@router.post("/download-zip")
async def download_zip(
    payload: DownloadZipPayload,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    """Gera e baixa um ZIP com os PDFs das certidões selecionadas."""
    if not payload.ids:
        raise HTTPException(400, "Selecione ao menos uma certidão.")

    stmt = select(Certidao).where(
        Certidao.escritorio_id == escritorio.id,
        Certidao.id.in_(payload.ids),
    )
    result = await db.execute(stmt)
    certs = result.scalars().all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for cert in certs:
            if cert.arquivo_path and os.path.exists(cert.arquivo_path):
                nome = os.path.basename(cert.arquivo_path)
                zf.write(cert.arquivo_path, nome)

    buf.seek(0)
    if buf.getbuffer().nbytes == 0:
        raise HTTPException(404, "Nenhuma das certidões selecionadas possui PDF disponível.")

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=certidoes.zip"},
    )
