from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List
from datetime import date, timedelta
import os

from ..database import get_db
from ..models import Cliente, Escritorio, Nota, StatusNotaEnum
from ..schemas import ClienteCreate, ClienteUpdate, ClienteResponse
from ..auth import get_usuario_atual, get_escritorio_atual
from ..models import Usuario, RoleEnum
from ..config import settings

router = APIRouter(prefix="/api/clientes", tags=["clientes"])


@router.get("", response_model=List[ClienteResponse])
async def listar_clientes(
    escritorio: Escritorio = Depends(get_escritorio_atual),
    usuario: Usuario = Depends(get_usuario_atual),
    db: AsyncSession = Depends(get_db),
):
    q = select(Cliente).where(Cliente.escritorio_id == escritorio.id, Cliente.ativo == True)
    # Contadores só vêem sua carteira; admins vêem todos
    is_admin = usuario.role == RoleEnum.admin or str(usuario.role) == "admin"
    if not is_admin:
        q = q.where(
            (Cliente.responsavel_id == usuario.id) | (Cliente.responsavel_id == None)
        )
    result = await db.execute(q.order_by(Cliente.razao_social))
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


@router.get("/certificados-vencendo")
async def certificados_vencendo(
    dias: int = Query(default=60, ge=1, le=365),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Retorna clientes com certificados vencidos ou prestes a vencer."""
    hoje = date.today()
    limite = hoje + timedelta(dias)
    result = await db.execute(
        select(Cliente).where(Cliente.escritorio_id == escritorio.id, Cliente.ativo == True)
    )
    alertas = []
    for c in result.scalars().all():
        for tipo, path_attr, venc_attr in [
            ("NFS-e", "nfse_certificado_path", "nfse_certificado_vencimento"),
            ("NF-e",  "nfe_certificado_path",  "nfe_certificado_vencimento"),
        ]:
            path = getattr(c, path_attr, None)
            venc = getattr(c, venc_attr, None)
            if not path or not venc:
                continue
            venc_date = venc.date() if hasattr(venc, "date") else venc
            if venc_date <= limite:
                dias_rest = (venc_date - hoje).days
                alertas.append({
                    "cliente_id": c.id,
                    "razao_social": c.razao_social,
                    "nome_fantasia": c.nome_fantasia,
                    "tipo": tipo,
                    "vencimento": venc_date.isoformat(),
                    "dias_restantes": dias_rest,
                    "vencido": dias_rest < 0,
                    "perigo": dias_rest <= 30,
                })
    alertas.sort(key=lambda x: x["vencimento"])
    return alertas


@router.post("/{cliente_id}/certificado/{tipo}")
async def upload_certificado(
    cliente_id: int,
    tipo: str,
    arquivo: UploadFile = File(...),
    senha: str = "",
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Faz upload de certificado A1 (.pfx) para NFS-e ou NF-e, extraindo a data de validade."""
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

    # Extrai data de validade do certificado
    vencimento_str = None
    vencimento_date = None
    try:
        from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12
        senha_bytes = senha.encode("utf-8") if senha else b""
        p12 = load_pkcs12(conteudo, senha_bytes)
        cert = p12.cert.certificate if p12.cert else None
        if cert:
            try:
                dt = cert.not_valid_after_utc
            except AttributeError:
                import datetime as _dt
                dt = cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
            vencimento_date = dt.date()
            vencimento_str = vencimento_date.isoformat()
    except Exception:
        pass  # senha errada ou cert inválido — salva sem vencimento

    if tipo == "nfse":
        cliente.nfse_certificado_path = caminho
        if vencimento_date:
            cliente.nfse_certificado_vencimento = vencimento_date
    else:
        cliente.nfe_certificado_path = caminho
        if vencimento_date:
            cliente.nfe_certificado_vencimento = vencimento_date

    await db.commit()
    return {"ok": True, "caminho": caminho, "nome": nome, "tamanho": len(conteudo), "vencimento": vencimento_str}


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
