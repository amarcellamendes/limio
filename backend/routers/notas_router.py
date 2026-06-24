from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import List, Optional
from datetime import datetime, timezone
import io

from ..database import get_db
from ..models import (
    Nota, Cliente, Escritorio, ItemNota,
    TipoNotaEnum, StatusNotaEnum,
)
from ..schemas import (
    EmitirNFSeRequest, EmitirNFeRequest,
    CancelarNotaRequest, NotaResponse, DashboardResponse, EscritorioResponse,
)
from ..auth import get_escritorio_atual, LIMITES_PLANO
from ..services.nfse_service import emitir_nfse, cancelar_nfse
from ..services.nfe_service import emitir_nfe, cancelar_nfe
from ..services.pdf_service import gerar_pdf_nfse, gerar_pdf_nfe, gerar_relatorio_apuracao
from ..services.boleto_service import emitir_boleto

router = APIRouter(prefix="/api/notas", tags=["notas"])


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    hoje = datetime.now(timezone.utc).date()
    mes_atual = hoje.strftime("%Y-%m")

    total_clientes = await db.execute(
        select(func.count()).select_from(Cliente).where(
            Cliente.escritorio_id == escritorio.id, Cliente.ativo == True
        )
    )
    notas_hoje = await db.execute(
        select(func.count(Nota.id)).where(
            Nota.escritorio_id == escritorio.id,
            func.date(Nota.criado_em) == str(hoje),
        )
    )
    notas_mes = await db.execute(
        select(func.count(Nota.id)).where(
            Nota.escritorio_id == escritorio.id,
            Nota.data_competencia == mes_atual,
            Nota.status == StatusNotaEnum.emitida,
        )
    )
    valor_mes = await db.execute(
        select(func.coalesce(func.sum(Nota.valor_servico), 0)).where(
            Nota.escritorio_id == escritorio.id,
            Nota.data_competencia == mes_atual,
            Nota.status == StatusNotaEnum.emitida,
        )
    )
    ultimas = await db.execute(
        select(Nota, Cliente.razao_social)
        .join(Cliente, Nota.cliente_id == Cliente.id)
        .where(Nota.escritorio_id == escritorio.id)
        .order_by(Nota.criado_em.desc())
        .limit(10)
    )

    limite = LIMITES_PLANO.get(escritorio.plano.value, 10)
    nm = notas_mes.scalar() or 0
    pct = round(nm / limite * 100, 1) if limite < 999_999 else 0.0

    ultimas_notas = []
    for row in ultimas.all():
        n, razao = row
        nr = NotaResponse.model_validate(n)
        nr.cliente_razao_social = razao
        ultimas_notas.append(nr)

    return DashboardResponse(
        escritorio=EscritorioResponse.model_validate(escritorio),
        total_clientes=total_clientes.scalar() or 0,
        notas_hoje=notas_hoje.scalar() or 0,
        notas_mes=nm,
        valor_mes=float(valor_mes.scalar() or 0),
        limite_mes=limite,
        percentual_uso=pct,
        ultimas_notas=ultimas_notas,
    )


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------

@router.get("", response_model=List[NotaResponse])
async def listar_notas(
    tipo: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    cliente_id: Optional[int] = Query(None),
    competencia: Optional[str] = Query(None),
    skip: int = 0,
    limit: int = 50,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Nota, Cliente.razao_social)
        .join(Cliente, Nota.cliente_id == Cliente.id)
        .where(Nota.escritorio_id == escritorio.id)
    )
    if tipo:
        q = q.where(Nota.tipo == tipo)
    if status:
        q = q.where(Nota.status == status)
    if cliente_id:
        q = q.where(Nota.cliente_id == cliente_id)
    if competencia:
        q = q.where(Nota.data_competencia == competencia)

    q = q.order_by(Nota.criado_em.desc()).offset(skip).limit(limit)
    result = await db.execute(q)

    notas = []
    for row in result.all():
        n, razao = row
        nr = NotaResponse.model_validate(n)
        nr.cliente_razao_social = razao
        notas.append(nr)
    return notas


@router.get("/{nota_id}", response_model=NotaResponse)
async def obter_nota(
    nota_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    nota = await _get_nota_ou_404(nota_id, escritorio.id, db)
    nr = NotaResponse.model_validate(nota)
    cliente = await db.get(Cliente, nota.cliente_id)
    if cliente:
        nr.cliente_razao_social = cliente.razao_social
    return nr


# ---------------------------------------------------------------------------
# Emissão NFS-e
# ---------------------------------------------------------------------------

@router.post("/nfse", response_model=NotaResponse, status_code=201)
async def emitir_nfse_endpoint(
    payload: EmitirNFSeRequest,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    cliente = await _get_cliente_ou_404_esc(payload.cliente_id, escritorio.id, db)
    await _verificar_limite(escritorio, db)
    nota = await _criar_nota_nfse(payload, cliente, escritorio, db)
    nr = NotaResponse.model_validate(nota)
    nr.cliente_razao_social = cliente.razao_social
    return nr


async def _criar_nota_nfse(
    payload: EmitirNFSeRequest,
    cliente: Cliente,
    escritorio: Escritorio,
    db: AsyncSession,
) -> Nota:
    """Core de criação de NFS-e — reutilizável pelo router de contratos."""
    cliente.nfse_ultimo_numero_rps += 1
    aliquota = payload.aliquota_iss or cliente.nfse_aliquota_iss or 5.0
    valor_iss = round(
        (payload.valor_servico - payload.valor_deducoes) * aliquota / 100, 2
    )
    valor_liquido = round(
        payload.valor_servico - valor_iss
        - payload.valor_pis - payload.valor_cofins
        - payload.valor_inss - payload.valor_ir - payload.valor_csll,
        2,
    )

    nota = Nota(
        escritorio_id=escritorio.id,
        cliente_id=cliente.id,
        tipo=TipoNotaEnum.nfse,
        status=StatusNotaEnum.pendente,
        serie=cliente.nfse_serie_rps or "RPS",
        numero_rps=cliente.nfse_ultimo_numero_rps,
        data_competencia=payload.data_competencia,
        tomador_cpf_cnpj=payload.tomador.cpf_cnpj,
        tomador_razao_social=payload.tomador.razao_social,
        tomador_email=payload.tomador.email,
        tomador_logradouro=payload.tomador.logradouro,
        tomador_numero=payload.tomador.numero,
        tomador_complemento=payload.tomador.complemento,
        tomador_bairro=payload.tomador.bairro,
        tomador_municipio=payload.tomador.municipio,
        tomador_codigo_ibge=payload.tomador.codigo_ibge,
        tomador_uf=payload.tomador.uf,
        tomador_cep=payload.tomador.cep,
        valor_servico=payload.valor_servico,
        valor_deducoes=payload.valor_deducoes,
        aliquota_iss=aliquota,
        valor_iss=valor_iss,
        valor_pis=payload.valor_pis,
        valor_cofins=payload.valor_cofins,
        valor_inss=payload.valor_inss,
        valor_ir=payload.valor_ir,
        valor_csll=payload.valor_csll,
        valor_liquido=valor_liquido,
        codigo_servico_lc116=payload.codigo_servico_lc116,
        codigo_servico_municipal=payload.codigo_servico_municipal,
        discriminacao=payload.discriminacao,
        iss_retido=payload.iss_retido,
    )
    db.add(nota)
    await db.flush()

    try:
        resultado = await emitir_nfse(payload, cliente, nota)
        nota.status = StatusNotaEnum.emitida
        nota.numero = resultado.get("numero")
        nota.chave_acesso = resultado.get("chave_acesso")
        nota.data_emissao = datetime.now(timezone.utc)
        nota.provider = resultado.get("provider")
        nota.provider_id = resultado.get("provider_id")
        nota.provider_status = resultado.get("provider_status")
        nota.xml_content = resultado.get("xml_content")
        nota.link_pdf = resultado.get("link_pdf")
        nota.link_xml = resultado.get("link_xml")
        if resultado.get("valor_iss"):
            nota.valor_iss = resultado["valor_iss"]
        if resultado.get("valor_liquido"):
            nota.valor_liquido = resultado["valor_liquido"]
        await _incrementar_uso(escritorio, db)
    except Exception as exc:
        nota.status = StatusNotaEnum.erro
        nota.erro_mensagem = str(exc)

    await db.commit()
    await db.refresh(nota)

    if nota.status == StatusNotaEnum.erro:
        raise HTTPException(422, detail=nota.erro_mensagem)

    return nota


# ---------------------------------------------------------------------------
# Emissão NF-e
# ---------------------------------------------------------------------------

@router.post("/nfe", response_model=NotaResponse, status_code=201)
async def emitir_nfe_endpoint(
    payload: EmitirNFeRequest,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    cliente = await _get_cliente_ou_404_esc(payload.cliente_id, escritorio.id, db)
    await _verificar_limite(escritorio, db)

    cliente.nfe_ultimo_numero += 1
    total_produtos = round(sum(i.quantidade * i.valor_unitario for i in payload.itens), 2)

    nota = Nota(
        escritorio_id=escritorio.id,
        cliente_id=cliente.id,
        tipo=TipoNotaEnum.nfe,
        status=StatusNotaEnum.pendente,
        serie=cliente.nfe_serie or "1",
        data_competencia=payload.data_competencia,
        tomador_cpf_cnpj=payload.tomador.cpf_cnpj,
        tomador_razao_social=payload.tomador.razao_social,
        tomador_email=payload.tomador.email,
        tomador_logradouro=payload.tomador.logradouro,
        tomador_numero=payload.tomador.numero,
        tomador_complemento=payload.tomador.complemento,
        tomador_bairro=payload.tomador.bairro,
        tomador_municipio=payload.tomador.municipio,
        tomador_codigo_ibge=payload.tomador.codigo_ibge,
        tomador_uf=payload.tomador.uf,
        tomador_cep=payload.tomador.cep,
        valor_servico=total_produtos,
        valor_liquido=total_produtos,
        natureza_operacao=payload.natureza_operacao,
    )
    db.add(nota)
    await db.flush()

    for idx, item in enumerate(payload.itens, 1):
        val = round(item.quantidade * item.valor_unitario, 2)
        db.add(ItemNota(
            nota_id=nota.id,
            numero_item=idx,
            codigo_produto=item.codigo_produto,
            descricao=item.descricao,
            ncm=item.ncm,
            cfop=item.cfop,
            unidade=item.unidade,
            quantidade=item.quantidade,
            valor_unitario=item.valor_unitario,
            valor_total=val,
            aliquota_icms=item.aliquota_icms,
            valor_icms=round(val * item.aliquota_icms / 100, 2),
            aliquota_pis=item.aliquota_pis,
            valor_pis=round(val * item.aliquota_pis / 100, 2),
            aliquota_cofins=item.aliquota_cofins,
            valor_cofins=round(val * item.aliquota_cofins / 100, 2),
        ))

    try:
        resultado = await emitir_nfe(payload, cliente, nota)
        nota.status = StatusNotaEnum.emitida
        nota.numero = resultado.get("numero")
        nota.chave_acesso = resultado.get("chave_acesso")
        nota.data_emissao = datetime.now(timezone.utc)
        nota.provider = resultado.get("provider")
        nota.provider_id = resultado.get("provider_id")
        nota.provider_status = resultado.get("provider_status")
        nota.xml_content = resultado.get("xml_content")
        nota.link_pdf = resultado.get("link_pdf")
        nota.link_xml = resultado.get("link_xml")
        await _incrementar_uso(escritorio, db)

    except NotImplementedError as exc:
        nota.status = StatusNotaEnum.erro
        nota.erro_mensagem = str(exc)
    except Exception as exc:
        nota.status = StatusNotaEnum.erro
        nota.erro_mensagem = str(exc)

    await db.commit()
    await db.refresh(nota)

    if nota.status == StatusNotaEnum.erro:
        raise HTTPException(422, detail=nota.erro_mensagem)

    nr = NotaResponse.model_validate(nota)
    nr.cliente_razao_social = cliente.razao_social
    return nr


# ---------------------------------------------------------------------------
# Cancelamento
# ---------------------------------------------------------------------------

@router.post("/{nota_id}/cancelar", response_model=NotaResponse)
async def cancelar_nota(
    nota_id: int,
    payload: CancelarNotaRequest,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    nota = await _get_nota_ou_404(nota_id, escritorio.id, db)

    if nota.status != StatusNotaEnum.emitida:
        raise HTTPException(400, "Apenas notas emitidas podem ser canceladas.")

    cliente = await db.get(Cliente, nota.cliente_id)

    try:
        if nota.tipo == TipoNotaEnum.nfse:
            await cancelar_nfse(nota, payload.motivo, cliente)
        else:
            await cancelar_nfe(nota, payload.motivo, cliente)

        nota.status = StatusNotaEnum.cancelada
        nota.motivo_cancelamento = payload.motivo
        nota.data_cancelamento = datetime.now(timezone.utc)
    except Exception as exc:
        raise HTTPException(422, str(exc))

    await db.commit()
    await db.refresh(nota)
    nr = NotaResponse.model_validate(nota)
    nr.cliente_razao_social = cliente.razao_social if cliente else None
    return nr


# ---------------------------------------------------------------------------
# Download PDF
# ---------------------------------------------------------------------------

@router.get("/{nota_id}/pdf")
async def download_pdf(
    nota_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    nota = await _get_nota_ou_404(nota_id, escritorio.id, db)
    cliente = await db.get(Cliente, nota.cliente_id)

    nota_data = _nota_para_dict(nota, cliente)

    if nota.tipo == TipoNotaEnum.nfse:
        pdf_bytes = gerar_pdf_nfse(nota_data)
    else:
        pdf_bytes = gerar_pdf_nfe(nota_data)

    nome_arquivo = f"nota_{nota.numero or nota.id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


# ---------------------------------------------------------------------------
# Download XML
# ---------------------------------------------------------------------------

@router.get("/{nota_id}/xml")
async def download_xml(
    nota_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    nota = await _get_nota_ou_404(nota_id, escritorio.id, db)
    if not nota.xml_content:
        raise HTTPException(404, "XML não disponível para esta nota.")

    nome_arquivo = f"nota_{nota.numero or nota.id}.xml"
    return Response(
        content=nota.xml_content.encode("utf-8"),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


# ---------------------------------------------------------------------------
# Duplicar nota — retorna dados para pré-preencher o formulário
# ---------------------------------------------------------------------------

@router.get("/{nota_id}/duplicar")
async def duplicar_dados(
    nota_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    nota = await _get_nota_ou_404(nota_id, escritorio.id, db)
    itens_res = await db.execute(select(ItemNota).where(ItemNota.nota_id == nota_id))
    itens = itens_res.scalars().all()
    return {
        "tipo": nota.tipo,
        "cliente_id": nota.cliente_id,
        "data_competencia": nota.data_competencia,
        "codigo_servico_lc116": nota.codigo_servico_lc116,
        "codigo_servico_municipal": nota.codigo_servico_municipal,
        "discriminacao": nota.discriminacao,
        "valor_servico": nota.valor_servico,
        "valor_deducoes": nota.valor_deducoes,
        "aliquota_iss": nota.aliquota_iss,
        "iss_retido": nota.iss_retido,
        "valor_pis": nota.valor_pis,
        "valor_cofins": nota.valor_cofins,
        "valor_inss": nota.valor_inss,
        "valor_ir": nota.valor_ir,
        "valor_csll": nota.valor_csll,
        "natureza_operacao": nota.natureza_operacao,
        "cfop": nota.cfop,
        "tomador": {
            "cpf_cnpj": nota.tomador_cpf_cnpj,
            "razao_social": nota.tomador_razao_social,
            "email": nota.tomador_email,
            "logradouro": nota.tomador_logradouro,
            "numero": nota.tomador_numero,
            "complemento": nota.tomador_complemento,
            "bairro": nota.tomador_bairro,
            "municipio": nota.tomador_municipio,
            "codigo_ibge": nota.tomador_codigo_ibge,
            "uf": nota.tomador_uf,
            "cep": nota.tomador_cep,
        },
        "itens": [
            {
                "descricao": i.descricao, "codigo_produto": i.codigo_produto,
                "ncm": i.ncm, "cfop": i.cfop, "unidade": i.unidade,
                "quantidade": i.quantidade, "valor_unitario": i.valor_unitario,
                "valor_total": i.valor_total,
            }
            for i in itens
        ],
    }


# ---------------------------------------------------------------------------
# Substituir nota — cancela original + cria nota substituta
# ---------------------------------------------------------------------------

@router.post("/{nota_id}/substituir")
async def substituir_nota(
    nota_id: int,
    payload: CancelarNotaRequest,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    nota_orig = await _get_nota_ou_404(nota_id, escritorio.id, db)
    if nota_orig.status not in (StatusNotaEnum.emitida,):
        raise HTTPException(400, "Somente notas emitidas podem ser substituídas.")

    cli_res = await db.execute(select(Cliente).where(Cliente.id == nota_orig.cliente_id))
    cliente = cli_res.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado")

    # Cancela a original
    try:
        if nota_orig.tipo == TipoNotaEnum.nfse:
            await cancelar_nfse(nota_orig, cliente, payload.motivo or "Substituição de nota")
        else:
            await cancelar_nfe(nota_orig, cliente, payload.motivo or "Substituição de nota")
    except Exception:
        pass  # Se falhou no provedor, cancela localmente mesmo assim

    nota_orig.status = StatusNotaEnum.cancelada
    nota_orig.motivo_cancelamento = payload.motivo or "Substituída"
    nota_orig.data_cancelamento = datetime.now(timezone.utc)
    await db.commit()

    # Retorna dados da nota original para pré-preencher o formulário de nova emissão
    return {
        "mensagem": "Nota original cancelada. Preencha os dados da nota substituta.",
        "nota_cancelada_id": nota_id,
        "dados_preenchimento": await duplicar_dados(nota_id, escritorio, db),
    }


# ---------------------------------------------------------------------------
# Emitir boleto vinculado a uma nota
# ---------------------------------------------------------------------------

@router.post("/{nota_id}/boleto")
async def emitir_boleto_nota(
    nota_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    nota = await _get_nota_ou_404(nota_id, escritorio.id, db)
    if nota.status != StatusNotaEnum.emitida:
        raise HTTPException(400, "Boleto disponível apenas para notas emitidas.")

    cli_res = await db.execute(select(Cliente).where(Cliente.id == nota.cliente_id))
    cliente = cli_res.scalar_one_or_none()

    provider = (cliente.boleto_provider or "mock") if cliente else "mock"
    api_key = cliente.boleto_api_key if cliente else None
    dias = (cliente.boleto_dias_vencimento or 3) if cliente else 3

    nota_dict = {
        "id": nota.id, "numero": nota.numero, "numero_rps": nota.numero_rps,
        "valor_servico": nota.valor_servico, "valor_liquido": nota.valor_liquido,
        "discriminacao": nota.discriminacao,
    }
    tomador_dict = {
        "cpf_cnpj": nota.tomador_cpf_cnpj,
        "razao_social": nota.tomador_razao_social,
        "email": nota.tomador_email,
    }

    resultado = await emitir_boleto(provider, api_key, nota_dict, tomador_dict, dias)

    from datetime import date as _date
    nota.boleto_linha_digitavel = resultado.get("linha_digitavel")
    nota.boleto_codigo_barras   = resultado.get("codigo_barras")
    nota.boleto_url             = resultado.get("url")
    nota.boleto_status          = resultado.get("status", "pendente")
    if resultado.get("vencimento"):
        try:
            nota.boleto_vencimento = datetime.fromisoformat(resultado["vencimento"])
        except Exception:
            pass

    await db.commit()
    return {**resultado, "nota_id": nota_id}


# ---------------------------------------------------------------------------
# Relatório de Apuração Mensal
# ---------------------------------------------------------------------------

@router.get("/relatorio/{cliente_id}/{competencia}")
async def relatorio_apuracao(
    cliente_id: int,
    competencia: str,   # YYYY-MM
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    cli_res = await db.execute(
        select(Cliente).where(Cliente.id == cliente_id, Cliente.escritorio_id == escritorio.id)
    )
    cliente = cli_res.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado")

    result = await db.execute(
        select(Nota).where(
            Nota.escritorio_id == escritorio.id,
            Nota.cliente_id == cliente_id,
            Nota.data_competencia == competencia,
            Nota.status.in_([StatusNotaEnum.emitida]),
        ).order_by(Nota.data_emissao)
    )
    notas = result.scalars().all()

    notas_dict = [
        {
            "tipo": n.tipo, "numero": n.numero, "numero_rps": n.numero_rps,
            "data_emissao": n.data_emissao.isoformat() if n.data_emissao else None,
            "tomador_razao_social": n.tomador_razao_social,
            "valor_servico": n.valor_servico, "valor_iss": n.valor_iss,
            "valor_ir": n.valor_ir, "valor_inss": n.valor_inss,
            "valor_pis": n.valor_pis, "valor_cofins": n.valor_cofins,
            "valor_csll": n.valor_csll, "valor_liquido": n.valor_liquido,
            "status": n.status,
        }
        for n in notas
    ]

    esc_dict = {
        "nome": escritorio.nome, "cnpj": escritorio.cnpj,
        "email": escritorio.email, "crc": escritorio.crc or "",
    }
    cli_dict = {
        "razao_social": cliente.razao_social, "cnpj": cliente.cnpj,
        "municipio": cliente.municipio or "", "uf": cliente.uf or "",
    }

    pdf_bytes = gerar_relatorio_apuracao(esc_dict, cli_dict, competencia, notas_dict)
    nome = f"apuracao_{cliente.cnpj.replace('/','_').replace('.','').replace('-','')}_{competencia}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_nota_ou_404(nota_id: int, escritorio_id: int, db: AsyncSession) -> Nota:
    result = await db.execute(
        select(Nota).where(Nota.id == nota_id, Nota.escritorio_id == escritorio_id)
    )
    nota = result.scalar_one_or_none()
    if not nota:
        raise HTTPException(404, "Nota não encontrada.")
    return nota


async def _get_cliente_ou_404_esc(
    cliente_id: int, escritorio_id: int, db: AsyncSession
) -> Cliente:
    result = await db.execute(
        select(Cliente).where(
            Cliente.id == cliente_id,
            Cliente.escritorio_id == escritorio_id,
            Cliente.ativo == True,
        )
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Cliente não encontrado.")
    return c


async def _verificar_limite(escritorio: Escritorio, db: AsyncSession):
    from datetime import datetime
    mes = datetime.now().strftime("%Y-%m")
    if escritorio.mes_referencia != mes:
        escritorio.mes_referencia = mes
        escritorio.notas_emitidas_mes = 0

    limite = LIMITES_PLANO.get(escritorio.plano.value, 10)
    if escritorio.notas_emitidas_mes >= limite:
        raise HTTPException(
            402,
            f"Limite do plano {escritorio.plano.value} atingido "
            f"({limite} notas/mês). Faça upgrade para continuar.",
        )


async def _incrementar_uso(escritorio: Escritorio, db: AsyncSession):
    from datetime import datetime
    mes = datetime.now().strftime("%Y-%m")
    if escritorio.mes_referencia != mes:
        escritorio.mes_referencia = mes
        escritorio.notas_emitidas_mes = 0
    escritorio.notas_emitidas_mes += 1


def _nota_para_dict(nota: Nota, cliente: Optional[Cliente]) -> dict:
    return {
        "id": nota.id,
        "numero": nota.numero,
        "serie": nota.serie,
        "status": nota.status.value if nota.status else "emitida",
        "tipo": nota.tipo.value if nota.tipo else "nfse",
        "chave_acesso": nota.chave_acesso,
        "provider_id": nota.provider_id,
        "provider": nota.provider,
        "data_emissao": nota.data_emissao.isoformat() if nota.data_emissao else None,
        "data_competencia": nota.data_competencia,
        "discriminacao": nota.discriminacao,
        "codigo_servico_lc116": nota.codigo_servico_lc116,
        "codigo_servico_municipal": nota.codigo_servico_municipal,
        "iss_retido": nota.iss_retido,
        "natureza_operacao": nota.natureza_operacao,
        "tomador_cpf_cnpj": nota.tomador_cpf_cnpj,
        "tomador_razao_social": nota.tomador_razao_social,
        "tomador_email": nota.tomador_email,
        "tomador_logradouro": nota.tomador_logradouro,
        "tomador_numero": nota.tomador_numero,
        "tomador_bairro": nota.tomador_bairro,
        "tomador_municipio": nota.tomador_municipio,
        "tomador_uf": nota.tomador_uf,
        "tomador_cep": nota.tomador_cep,
        "valor_servico": nota.valor_servico,
        "valor_deducoes": nota.valor_deducoes,
        "aliquota_iss": nota.aliquota_iss,
        "valor_iss": nota.valor_iss,
        "valor_pis": nota.valor_pis,
        "valor_cofins": nota.valor_cofins,
        "valor_inss": nota.valor_inss,
        "valor_ir": nota.valor_ir,
        "valor_csll": nota.valor_csll,
        "valor_liquido": nota.valor_liquido,
        "cliente_razao_social": cliente.razao_social if cliente else None,
        "cliente_cnpj": cliente.cnpj if cliente else None,
        "cliente_municipio": cliente.municipio if cliente else None,
        "cliente_uf": cliente.uf if cliente else None,
        "cliente_im": cliente.im if cliente else None,
    }
