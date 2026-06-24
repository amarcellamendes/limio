from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List

from ..database import get_db
from ..models import Cliente, Escritorio, Nota, StatusNotaEnum
from ..schemas import ClienteCreate, ClienteUpdate, ClienteResponse
from ..auth import get_usuario_atual, get_escritorio_atual
from ..models import Usuario

router = APIRouter(prefix="/api/clientes", tags=["clientes"])


@router.get("", response_model=List[ClienteResponse])
async def listar_clientes(
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Cliente)
        .where(Cliente.escritorio_id == escritorio.id, Cliente.ativo == True)
        .order_by(Cliente.razao_social)
    )
    return result.scalars().all()


@router.post("", response_model=ClienteResponse, status_code=201)
async def criar_cliente(
    payload: ClienteCreate,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    dup = await db.execute(
        select(Cliente).where(
            Cliente.escritorio_id == escritorio.id,
            Cliente.cnpj == payload.cnpj,
            Cliente.ativo == True,
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(400, "CNPJ já cadastrado neste escritório.")

    cliente = Cliente(escritorio_id=escritorio.id, **payload.model_dump())
    db.add(cliente)
    await db.commit()
    await db.refresh(cliente)
    return cliente


@router.get("/{cliente_id}", response_model=ClienteResponse)
async def obter_cliente(
    cliente_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    return await _get_cliente_ou_404(cliente_id, escritorio.id, db)


@router.put("/{cliente_id}", response_model=ClienteResponse)
async def atualizar_cliente(
    cliente_id: int,
    payload: ClienteUpdate,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    cliente = await _get_cliente_ou_404(cliente_id, escritorio.id, db)
    for campo, valor in payload.model_dump(exclude_none=True).items():
        setattr(cliente, campo, valor)
    await db.commit()
    await db.refresh(cliente)
    return cliente


@router.delete("/{cliente_id}", status_code=204)
async def desativar_cliente(
    cliente_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    cliente = await _get_cliente_ou_404(cliente_id, escritorio.id, db)
    cliente.ativo = False
    await db.commit()


@router.get("/{cliente_id}/resumo")
async def resumo_cliente(
    cliente_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    cliente = await _get_cliente_ou_404(cliente_id, escritorio.id, db)

    total = await db.execute(
        select(func.count(Nota.id)).where(Nota.cliente_id == cliente_id)
    )
    emitidas = await db.execute(
        select(func.count(Nota.id)).where(
            Nota.cliente_id == cliente_id,
            Nota.status == StatusNotaEnum.emitida,
        )
    )
    valor = await db.execute(
        select(func.sum(Nota.valor_servico)).where(
            Nota.cliente_id == cliente_id,
            Nota.status == StatusNotaEnum.emitida,
        )
    )

    return {
        "cliente_id": cliente_id,
        "razao_social": cliente.razao_social,
        "total_notas": total.scalar() or 0,
        "notas_emitidas": emitidas.scalar() or 0,
        "valor_total_emitido": valor.scalar() or 0.0,
    }


async def _get_cliente_ou_404(
    cliente_id: int, escritorio_id: int, db: AsyncSession
) -> Cliente:
    result = await db.execute(
        select(Cliente).where(
            Cliente.id == cliente_id,
            Cliente.escritorio_id == escritorio_id,
        )
    )
    cliente = result.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")
    return cliente
