"""
Router de contratos recorrentes.

Contratos armazenam dados de NFS-e/NF-e de valor fixo mensal.
No dia configurado, entram em fila (para aprovação manual) ou são emitidos automaticamente.
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from datetime import datetime, timezone, date
from typing import Optional
import calendar

from ..database import get_db
from ..models import (
    Contrato, Cliente, Escritorio, Nota,
    TipoNotaEnum, StatusNotaEnum, ModoEmissaoEnum,
)
from ..auth import get_escritorio_atual

router = APIRouter(prefix="/api/contratos", tags=["contratos"])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def listar_contratos(
    ativo: Optional[bool] = None,
    cliente_id: Optional[int] = None,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    q = select(Contrato).where(Contrato.escritorio_id == escritorio.id)
    if ativo is not None:
        q = q.where(Contrato.ativo == ativo)
    if cliente_id:
        q = q.where(Contrato.cliente_id == cliente_id)
    q = q.order_by(Contrato.dia_emissao, Contrato.descricao)
    r = await db.execute(q)
    contratos = r.scalars().all()

    hoje = date.today()
    result = []
    for c in contratos:
        cli_r = await db.execute(select(Cliente).where(Cliente.id == c.cliente_id))
        cli = cli_r.scalar_one_or_none()
        prox = _proxima_data(c.dia_emissao, hoje)
        result.append({
            "id": c.id, "descricao": c.descricao, "valor": c.valor,
            "dia_emissao": c.dia_emissao, "tipo_nota": c.tipo_nota,
            "modo_emissao": c.modo_emissao, "gerar_boleto": c.gerar_boleto,
            "ativo": c.ativo, "total_emitido": c.total_emitido,
            "proxima_emissao": prox.isoformat(),
            "vence_hoje": prox == hoje,
            "cliente_id": c.cliente_id,
            "cliente_razao_social": cli.razao_social if cli else "—",
            "ultima_nota_id": c.ultima_nota_id,
            "aliquota_iss": c.aliquota_iss,
            "codigo_servico_lc116": c.codigo_servico_lc116,
            "discriminacao": c.discriminacao,
            "tomador_cpf_cnpj": c.tomador_cpf_cnpj,
            "tomador_razao_social": c.tomador_razao_social,
            "tomador_email": c.tomador_email,
            "tomador_municipio": c.tomador_municipio,
            "tomador_uf": c.tomador_uf,
            "iss_retido": c.iss_retido,
            "valor_deducoes": c.valor_deducoes,
            "valor_pis": c.valor_pis, "valor_cofins": c.valor_cofins,
            "valor_inss": c.valor_inss, "valor_ir": c.valor_ir, "valor_csll": c.valor_csll,
            "criado_em": c.criado_em.isoformat() if c.criado_em else None,
        })
    return result


@router.post("", status_code=201)
async def criar_contrato(
    payload: dict,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    cliente_id = payload.get("cliente_id")
    if not cliente_id:
        raise HTTPException(400, "cliente_id obrigatório.")
    cli = await db.get(Cliente, cliente_id)
    if not cli or cli.escritorio_id != escritorio.id:
        raise HTTPException(404, "Cliente não encontrado.")

    dia = int(payload.get("dia_emissao", 1))
    if not 1 <= dia <= 28:
        raise HTTPException(400, "dia_emissao deve ser entre 1 e 28.")

    contrato = Contrato(
        escritorio_id=escritorio.id,
        cliente_id=cliente_id,
        descricao=payload.get("descricao", ""),
        tipo_nota=payload.get("tipo_nota", TipoNotaEnum.nfse),
        valor=float(payload.get("valor", 0)),
        aliquota_iss=float(payload.get("aliquota_iss") or cli.nfse_aliquota_iss or 5.0),
        iss_retido=bool(payload.get("iss_retido", False)),
        valor_deducoes=float(payload.get("valor_deducoes", 0)),
        valor_pis=float(payload.get("valor_pis", 0)),
        valor_cofins=float(payload.get("valor_cofins", 0)),
        valor_inss=float(payload.get("valor_inss", 0)),
        valor_ir=float(payload.get("valor_ir", 0)),
        valor_csll=float(payload.get("valor_csll", 0)),
        codigo_servico_lc116=payload.get("codigo_servico_lc116"),
        codigo_servico_municipal=payload.get("codigo_servico_municipal"),
        discriminacao=payload.get("discriminacao"),
        tomador_cpf_cnpj=payload.get("tomador_cpf_cnpj"),
        tomador_razao_social=payload.get("tomador_razao_social"),
        tomador_email=payload.get("tomador_email"),
        tomador_logradouro=payload.get("tomador_logradouro"),
        tomador_numero=payload.get("tomador_numero"),
        tomador_complemento=payload.get("tomador_complemento"),
        tomador_bairro=payload.get("tomador_bairro"),
        tomador_municipio=payload.get("tomador_municipio"),
        tomador_codigo_ibge=payload.get("tomador_codigo_ibge"),
        tomador_uf=payload.get("tomador_uf"),
        tomador_cep=payload.get("tomador_cep"),
        dia_emissao=dia,
        modo_emissao=payload.get("modo_emissao", ModoEmissaoEnum.fila),
        gerar_boleto=bool(payload.get("gerar_boleto", False)),
        ativo=True,
        total_emitido=0,
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    return {"id": contrato.id, "descricao": contrato.descricao, "ativo": True}


@router.put("/{contrato_id}")
async def atualizar_contrato(
    contrato_id: int,
    payload: dict,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    c = await _get_contrato(contrato_id, escritorio.id, db)
    campos = [
        "descricao", "valor", "dia_emissao", "tipo_nota", "modo_emissao",
        "gerar_boleto", "ativo", "aliquota_iss", "iss_retido", "discriminacao",
        "codigo_servico_lc116", "codigo_servico_municipal",
        "tomador_cpf_cnpj", "tomador_razao_social", "tomador_email",
        "tomador_logradouro", "tomador_numero", "tomador_bairro",
        "tomador_municipio", "tomador_codigo_ibge", "tomador_uf", "tomador_cep",
        "valor_deducoes", "valor_pis", "valor_cofins", "valor_inss", "valor_ir", "valor_csll",
    ]
    for campo in campos:
        if campo in payload and payload[campo] is not None:
            setattr(c, campo, payload[campo])
    await db.commit()
    await db.refresh(c)
    return {"id": c.id, "descricao": c.descricao, "ativo": c.ativo}


@router.delete("/{contrato_id}", status_code=204)
async def desativar_contrato(
    contrato_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    c = await _get_contrato(contrato_id, escritorio.id, db)
    c.ativo = False
    await db.commit()


# ---------------------------------------------------------------------------
# Emissão
# ---------------------------------------------------------------------------

@router.post("/{contrato_id}/emitir")
async def emitir_contrato(
    contrato_id: int,
    background_tasks: BackgroundTasks,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Emite a nota do contrato imediatamente (modo manual ou via processamento automático)."""
    c = await _get_contrato(contrato_id, escritorio.id, db)
    cli_r = await db.execute(select(Cliente).where(Cliente.id == c.cliente_id))
    cliente = cli_r.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")

    nota = await _emitir_nota_do_contrato(c, cliente, escritorio, db)
    c.ultima_nota_id = nota.id
    c.total_emitido = (c.total_emitido or 0) + 1
    await db.commit()

    # Se gerar boleto, faz em background para não travar a resposta
    if c.gerar_boleto and cliente.boleto_ativo:
        from ..services.boleto_service import emitir_boleto
        async def _boleto():
            async with db as s:
                resultado = await emitir_boleto(
                    provider=cliente.boleto_provider or "mock",
                    api_key=cliente.boleto_api_key,
                    nota_data={"id": nota.id, "numero_rps": nota.numero_rps,
                               "valor_servico": nota.valor_servico, "discriminacao": nota.discriminacao},
                    tomador={"cpf_cnpj": nota.tomador_cpf_cnpj, "razao_social": nota.tomador_razao_social,
                             "email": nota.tomador_email},
                    dias_vencimento=cliente.boleto_dias_vencimento or 3,
                )
                nota.boleto_linha_digitavel = resultado.get("linha_digitavel")
                nota.boleto_url = resultado.get("url")
                nota.boleto_status = resultado.get("status", "pendente")
                await s.commit()
        background_tasks.add_task(_boleto)

    return {
        "nota_id": nota.id,
        "numero_rps": nota.numero_rps,
        "status": nota.status,
        "valor_servico": nota.valor_servico,
    }


@router.get("/fila")
async def contratos_na_fila(
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Contratos com modo=fila cujo dia_emissao é hoje."""
    hoje = date.today()
    q = select(Contrato).where(
        Contrato.escritorio_id == escritorio.id,
        Contrato.ativo == True,
        Contrato.modo_emissao == ModoEmissaoEnum.fila,
        Contrato.dia_emissao == hoje.day,
    )
    r = await db.execute(q)
    contratos = r.scalars().all()
    result = []
    for c in contratos:
        cli_r = await db.execute(select(Cliente).where(Cliente.id == c.cliente_id))
        cli = cli_r.scalar_one_or_none()
        result.append({
            "id": c.id, "descricao": c.descricao, "valor": c.valor,
            "cliente_razao_social": cli.razao_social if cli else "—",
            "tipo_nota": c.tipo_nota,
        })
    return result


@router.post("/processar-automaticos")
async def processar_automaticos(
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Emite automaticamente todos os contratos com modo=automatico cujo dia é hoje."""
    hoje = date.today()
    q = select(Contrato).where(
        Contrato.escritorio_id == escritorio.id,
        Contrato.ativo == True,
        Contrato.modo_emissao == ModoEmissaoEnum.automatico,
        Contrato.dia_emissao == hoje.day,
    )
    r = await db.execute(q)
    contratos = r.scalars().all()

    emitidas, erros = [], []
    for c in contratos:
        cli_r = await db.execute(select(Cliente).where(Cliente.id == c.cliente_id))
        cliente = cli_r.scalar_one_or_none()
        try:
            nota = await _emitir_nota_do_contrato(c, cliente, escritorio, db)
            c.ultima_nota_id = nota.id
            c.total_emitido = (c.total_emitido or 0) + 1
            await db.commit()
            emitidas.append({"contrato_id": c.id, "nota_id": nota.id})
        except Exception as e:
            erros.append({"contrato_id": c.id, "erro": str(e)})

    return {"emitidas": len(emitidas), "erros": len(erros), "detalhe_erros": erros}


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

async def _get_contrato(contrato_id: int, escritorio_id: int, db: AsyncSession) -> Contrato:
    r = await db.execute(
        select(Contrato).where(Contrato.id == contrato_id, Contrato.escritorio_id == escritorio_id)
    )
    c = r.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Contrato não encontrado.")
    return c


def _proxima_data(dia: int, a_partir: date) -> date:
    """Retorna a próxima data de emissão >= a_partir."""
    d = min(dia, calendar.monthrange(a_partir.year, a_partir.month)[1])
    candidato = a_partir.replace(day=d)
    if candidato < a_partir:
        # próximo mês
        if a_partir.month == 12:
            candidato = date(a_partir.year + 1, 1, min(dia, 28))
        else:
            ultimo_prox = calendar.monthrange(a_partir.year, a_partir.month + 1)[1]
            candidato = date(a_partir.year, a_partir.month + 1, min(dia, ultimo_prox))
    return candidato


async def _emitir_nota_do_contrato(
    c: Contrato, cliente: Cliente, escritorio: Escritorio, db: AsyncSession
) -> Nota:
    from ..schemas import EmitirNFSeRequest, TomadorSchema
    from .notas_router import _criar_nota_nfse

    hoje = datetime.now(timezone.utc)

    if c.tipo_nota == TipoNotaEnum.nfse:
        req = EmitirNFSeRequest(
            cliente_id=c.cliente_id,
            data_competencia=hoje.strftime("%Y-%m"),
            codigo_servico_lc116=c.codigo_servico_lc116 or "01.07",
            codigo_servico_municipal=c.codigo_servico_municipal or "",
            discriminacao=c.discriminacao or c.descricao,
            valor_servico=c.valor,
            valor_deducoes=c.valor_deducoes,
            aliquota_iss=c.aliquota_iss or 5.0,
            iss_retido=c.iss_retido,
            valor_pis=c.valor_pis,
            valor_cofins=c.valor_cofins,
            valor_inss=c.valor_inss,
            valor_ir=c.valor_ir,
            valor_csll=c.valor_csll,
            tomador=TomadorSchema(
                cpf_cnpj=c.tomador_cpf_cnpj or "",
                razao_social=c.tomador_razao_social or "",
                email=c.tomador_email,
                logradouro=c.tomador_logradouro,
                numero=c.tomador_numero,
                complemento=c.tomador_complemento,
                bairro=c.tomador_bairro,
                municipio=c.tomador_municipio,
                codigo_ibge=c.tomador_codigo_ibge,
                uf=c.tomador_uf,
                cep=c.tomador_cep,
            ),
        )
        return await _criar_nota_nfse(req, cliente, escritorio, db)

    raise HTTPException(400, f"Emissão automática de {c.tipo_nota} via contrato ainda não suportada.")
