"""
Gestão de usuários do escritório — CRUD + carteira de clientes.
Apenas admin pode criar/editar/desativar usuários.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional, List
from pydantic import BaseModel, EmailStr

from ..database import get_db
from ..models import Usuario, Cliente, Escritorio, RoleEnum
from ..auth import get_usuario_atual, get_escritorio_atual, hash_senha

router = APIRouter(prefix="/api/usuarios", tags=["Usuários"])


class UsuarioCreate(BaseModel):
    nome: str
    email: EmailStr
    senha: str
    role: str = "contador"


class UsuarioUpdate(BaseModel):
    nome: Optional[str] = None
    email: Optional[EmailStr] = None
    senha: Optional[str] = None
    role: Optional[str] = None
    ativo: Optional[bool] = None


class CarteiraUpdate(BaseModel):
    cliente_ids: List[int]


def _is_admin(usuario: Usuario) -> bool:
    return usuario.role == RoleEnum.admin or str(usuario.role) == "admin"


async def _require_admin(usuario: Usuario = Depends(get_usuario_atual)):
    if not _is_admin(usuario):
        raise HTTPException(403, "Apenas administradores podem gerenciar usuários.")
    return usuario


@router.get("")
async def listar_usuarios(
    escritorio: Escritorio = Depends(get_escritorio_atual),
    usuario: Usuario = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Usuario).where(Usuario.escritorio_id == escritorio.id).order_by(Usuario.nome)
    )
    usuarios = result.scalars().all()

    # Para cada usuário, conta quantos clientes estão na carteira
    saida = []
    for u in usuarios:
        qtd = await db.execute(
            select(Cliente.id).where(
                Cliente.escritorio_id == escritorio.id,
                Cliente.responsavel_id == u.id,
                Cliente.ativo == True,
            )
        )
        saida.append({
            "id": u.id,
            "nome": u.nome,
            "email": u.email,
            "role": u.role,
            "ativo": u.ativo,
            "criado_em": u.criado_em.isoformat() if u.criado_em else None,
            "qtd_clientes": len(qtd.scalars().all()),
        })
    return saida


@router.post("", status_code=201)
async def criar_usuario(
    payload: UsuarioCreate,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    _admin: Usuario = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    dup = await db.execute(select(Usuario).where(Usuario.email == payload.email))
    if dup.scalar_one_or_none():
        raise HTTPException(400, "E-mail já cadastrado.")

    role = RoleEnum.admin if payload.role == "admin" else RoleEnum.contador
    u = Usuario(
        escritorio_id=escritorio.id,
        nome=payload.nome,
        email=payload.email,
        senha_hash=hash_senha(payload.senha),
        role=role,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return {"id": u.id, "nome": u.nome, "email": u.email, "role": u.role, "ativo": u.ativo}


@router.put("/{usuario_id}")
async def atualizar_usuario(
    usuario_id: int,
    payload: UsuarioUpdate,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    admin: Usuario = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Usuario).where(Usuario.id == usuario_id, Usuario.escritorio_id == escritorio.id)
    )
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(404, "Usuário não encontrado.")

    if payload.nome is not None:
        u.nome = payload.nome
    if payload.email is not None:
        u.email = payload.email
    if payload.senha:
        u.senha_hash = hash_senha(payload.senha)
    if payload.role is not None:
        u.role = RoleEnum.admin if payload.role == "admin" else RoleEnum.contador
    if payload.ativo is not None:
        if u.id == admin.id and not payload.ativo:
            raise HTTPException(400, "Você não pode desativar sua própria conta.")
        u.ativo = payload.ativo

    await db.commit()
    return {"ok": True}


@router.put("/{usuario_id}/carteira")
async def definir_carteira(
    usuario_id: int,
    payload: CarteiraUpdate,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    _admin: Usuario = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Define quais clientes estão na carteira do usuário.
    Remove todos os que não estão na lista e adiciona os novos.
    """
    result = await db.execute(
        select(Usuario).where(Usuario.id == usuario_id, Usuario.escritorio_id == escritorio.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(404, "Usuário não encontrado.")

    # Remove responsável de quem estava na carteira e não está mais
    todos = await db.execute(
        select(Cliente).where(
            Cliente.escritorio_id == escritorio.id,
            Cliente.responsavel_id == usuario_id,
        )
    )
    for c in todos.scalars().all():
        if c.id not in payload.cliente_ids:
            c.responsavel_id = None

    # Atribui para os que estão na nova lista
    if payload.cliente_ids:
        novos = await db.execute(
            select(Cliente).where(
                Cliente.escritorio_id == escritorio.id,
                Cliente.id.in_(payload.cliente_ids),
            )
        )
        for c in novos.scalars().all():
            c.responsavel_id = usuario_id

    await db.commit()
    return {"ok": True, "atribuidos": len(payload.cliente_ids)}


@router.get("/{usuario_id}/carteira")
async def listar_carteira(
    usuario_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    _admin: Usuario = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Cliente).where(
            Cliente.escritorio_id == escritorio.id,
            Cliente.responsavel_id == usuario_id,
            Cliente.ativo == True,
        ).order_by(Cliente.razao_social)
    )
    clientes = result.scalars().all()
    return [{"id": c.id, "razao_social": c.razao_social, "nome_fantasia": c.nome_fantasia} for c in clientes]
