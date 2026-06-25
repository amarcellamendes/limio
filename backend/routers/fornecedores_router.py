"""
Router: Fornecedores — emitentes de notas recebidas.
Auto-cadastrados quando uma nota recebida é importada; editáveis manualmente.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_escritorio_atual
from ..database import get_db
from ..models import Escritorio, Fornecedor

router = APIRouter(prefix="/api/fornecedores", tags=["Fornecedores"])


class FornecedorUpdate(BaseModel):
    nome_fantasia: Optional[str] = None
    email: Optional[str] = None
    telefone: Optional[str] = None
    categoria: Optional[str] = None
    ativo: Optional[bool] = None


@router.get("")
async def listar_fornecedores(
    q: Optional[str] = Query(None, description="Busca por CNPJ ou razão social"),
    categoria: Optional[str] = Query(None),
    ativo: Optional[bool] = Query(None),
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    stmt = select(Fornecedor).where(Fornecedor.escritorio_id == escritorio.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Fornecedor.razao_social.ilike(like)) | (Fornecedor.cnpj.ilike(like))
        )
    if categoria:
        stmt = stmt.where(Fornecedor.categoria == categoria)
    if ativo is not None:
        stmt = stmt.where(Fornecedor.ativo == ativo)
    stmt = stmt.order_by(Fornecedor.razao_social)
    result = await db.execute(stmt)
    fornecedores = result.scalars().all()
    return [_to_dict(f) for f in fornecedores]


@router.get("/resumo")
async def resumo_fornecedores(
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    total_res = await db.execute(
        select(func.count(Fornecedor.id)).where(Fornecedor.escritorio_id == escritorio.id)
    )
    total = total_res.scalar() or 0

    valor_res = await db.execute(
        select(func.sum(Fornecedor.valor_total_notas)).where(
            Fornecedor.escritorio_id == escritorio.id
        )
    )
    valor = round(valor_res.scalar() or 0, 2)

    return {"total_fornecedores": total, "valor_total_compras": valor}


@router.get("/{fornecedor_id}")
async def obter_fornecedor(
    fornecedor_id: int,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    f = await _get_or_404(fornecedor_id, escritorio.id, db)
    return _to_dict(f)


@router.put("/{fornecedor_id}")
async def atualizar_fornecedor(
    fornecedor_id: int,
    payload: FornecedorUpdate,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    f = await _get_or_404(fornecedor_id, escritorio.id, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(f, field, value)
    f.atualizado_em = datetime.utcnow()
    await db.commit()
    await db.refresh(f)
    return _to_dict(f)


@router.delete("/{fornecedor_id}", status_code=204)
async def excluir_fornecedor(
    fornecedor_id: int,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    f = await _get_or_404(fornecedor_id, escritorio.id, db)
    await db.delete(f)
    await db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def upsert_fornecedor(
    db: AsyncSession,
    escritorio_id: int,
    cnpj: str,
    razao_social: str,
    uf: Optional[str] = None,
    municipio: Optional[str] = None,
    valor_nota: float = 0.0,
    data_nota: Optional[datetime] = None,
) -> Fornecedor:
    """Cria ou atualiza fornecedor ao importar nota recebida."""
    if not cnpj:
        return None
    cnpj_clean = cnpj.strip()
    result = await db.execute(
        select(Fornecedor).where(
            Fornecedor.escritorio_id == escritorio_id,
            Fornecedor.cnpj == cnpj_clean,
        )
    )
    f = result.scalar_one_or_none()
    if f is None:
        f = Fornecedor(
            escritorio_id=escritorio_id,
            cnpj=cnpj_clean,
            razao_social=razao_social or cnpj_clean,
            uf=uf,
            municipio=municipio,
            total_notas=1,
            valor_total_notas=valor_nota,
            ultima_nota_em=data_nota or datetime.utcnow(),
        )
        db.add(f)
    else:
        if razao_social and razao_social != f.razao_social:
            f.razao_social = razao_social
        if uf and not f.uf:
            f.uf = uf
        if municipio and not f.municipio:
            f.municipio = municipio
        f.total_notas = (f.total_notas or 0) + 1
        f.valor_total_notas = (f.valor_total_notas or 0) + valor_nota
        if data_nota and (not f.ultima_nota_em or data_nota > f.ultima_nota_em):
            f.ultima_nota_em = data_nota
        f.atualizado_em = datetime.utcnow()
    return f


async def _get_or_404(fornecedor_id: int, escritorio_id: int, db: AsyncSession) -> Fornecedor:
    result = await db.execute(
        select(Fornecedor).where(
            Fornecedor.id == fornecedor_id,
            Fornecedor.escritorio_id == escritorio_id,
        )
    )
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(404, f"Fornecedor {fornecedor_id} não encontrado.")
    return f


def _to_dict(f: Fornecedor) -> dict:
    return {
        "id": f.id,
        "cnpj": f.cnpj,
        "razao_social": f.razao_social,
        "nome_fantasia": f.nome_fantasia,
        "uf": f.uf,
        "municipio": f.municipio,
        "email": f.email,
        "telefone": f.telefone,
        "categoria": f.categoria,
        "ativo": f.ativo,
        "total_notas": f.total_notas,
        "valor_total_notas": f.valor_total_notas,
        "ultima_nota_em": f.ultima_nota_em.isoformat() if f.ultima_nota_em else None,
        "criado_em": f.criado_em.isoformat() if f.criado_em else None,
    }
