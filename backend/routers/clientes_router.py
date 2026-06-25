from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List
import os

from ..database import get_db
from ..models import Cliente, Escritorio, Nota, StatusNotaEnum
from ..schemas import ClienteCreate, ClienteUpdate, ClienteResponse
from ..auth import get_usuario_atual, get_escritorio_atual
from ..models import Usuario
from ..config import settings

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


@router.post("/{cliente_id}/certificado/{tipo}")
async def upload_certificado(
    cliente_id: int,
    tipo: str,
    arquivo: UploadFile = File(...),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Faz upload de certificado A1 (.pfx) para NFS-e ou NF-e."""
    if tipo not in ("nfse", "nfe"):
        raise HTTPException(400, "Tipo deve ser 'nfse' ou 'nfe'.")
    if not arquivo.filename.lower().endswith(".pfx"):
        raise HTTPException(400, "Apenas arquivos .pfx são aceitos.")

    cliente = await _get_cliente_ou_404(cliente_id, escritorio.id, db)

    pasta = os.path.join(settings.DATA_DIR, "certs", str(escritorio.id), str(cliente_id))
    os.makedirs(pasta, exist_ok=True)

    nome = f"{tipo}_cert.pfx"
    caminho = os.path.join(pasta, nome)

    conteudo = await arquivo.read()
    with open(caminho, "wb") as f:
        f.write(conteudo)

    if tipo == "nfse":
        cliente.nfse_certificado_path = caminho
    else:
        cliente.nfe_certificado_path = caminho

    await db.commit()
    return {"ok": True, "caminho": caminho, "nome": nome, "tamanho": len(conteudo)}


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
