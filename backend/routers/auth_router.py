from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..database import get_db
from ..models import Escritorio, Usuario, PlanoEnum
from ..schemas import EscritorioCreate, LoginRequest, TokenResponse, EscritorioResponse
from ..auth import (
    hash_senha, verificar_senha, criar_token,
    get_usuario_atual, get_escritorio_atual, LIMITES_PLANO,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/registrar", response_model=TokenResponse, status_code=201)
async def registrar(payload: EscritorioCreate, db: AsyncSession = Depends(get_db)):
    # Verifica duplicidade de CNPJ e e-mail
    dup = await db.execute(
        select(Escritorio).where(
            (Escritorio.cnpj == payload.cnpj) | (Escritorio.email == payload.email)
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(400, "CNPJ ou e-mail já cadastrado.")

    dup_user = await db.execute(
        select(Usuario).where(Usuario.email == payload.usuario_email)
    )
    if dup_user.scalar_one_or_none():
        raise HTTPException(400, "E-mail de usuário já cadastrado.")

    escritorio = Escritorio(
        nome=payload.nome,
        cnpj=payload.cnpj,
        email=payload.email,
        telefone=payload.telefone,
        crc=payload.crc,
        plano=PlanoEnum.free,
        limite_notas_mes=LIMITES_PLANO["free"],
    )
    db.add(escritorio)
    await db.flush()

    usuario = Usuario(
        escritorio_id=escritorio.id,
        nome=payload.usuario_nome,
        email=payload.usuario_email,
        senha_hash=hash_senha(payload.senha),
        role="admin",
    )
    db.add(usuario)
    await db.commit()
    await db.refresh(usuario)
    await db.refresh(escritorio)

    token = criar_token({"sub": str(usuario.id)})
    return TokenResponse(
        access_token=token,
        usuario_nome=usuario.nome,
        escritorio_nome=escritorio.nome,
        escritorio_id=escritorio.id,
        plano=escritorio.plano,
        role=usuario.role,
    )


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Usuario).where(Usuario.email == payload.email, Usuario.ativo == True)
    )
    usuario = result.scalar_one_or_none()

    if not usuario or not verificar_senha(payload.senha, usuario.senha_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="E-mail ou senha incorretos.",
        )

    result_esc = await db.execute(
        select(Escritorio).where(Escritorio.id == usuario.escritorio_id)
    )
    escritorio = result_esc.scalar_one()

    token = criar_token({"sub": str(usuario.id)})
    return TokenResponse(
        access_token=token,
        usuario_nome=usuario.nome,
        escritorio_nome=escritorio.nome,
        escritorio_id=escritorio.id,
        plano=escritorio.plano,
        role=usuario.role,
    )


@router.get("/me", response_model=EscritorioResponse)
async def me(
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    return escritorio
