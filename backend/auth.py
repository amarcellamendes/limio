from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .config import settings
from .database import get_db
from .models import Usuario, Escritorio

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verificar_senha(senha: str, hash_str: str) -> bool:
    return bcrypt.checkpw(senha.encode("utf-8"), hash_str.encode("utf-8"))


def criar_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    payload = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload.update({"exp": expire})
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


async def get_usuario_atual(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    credenciais_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciais inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        usuario_id: Optional[int] = payload.get("sub")
        if usuario_id is None:
            raise credenciais_exc
    except JWTError:
        raise credenciais_exc

    result = await db.execute(
        select(Usuario).where(Usuario.id == int(usuario_id), Usuario.ativo == True)
    )
    usuario = result.scalar_one_or_none()
    if usuario is None:
        raise credenciais_exc
    return usuario


async def get_escritorio_atual(
    usuario: Usuario = Depends(get_usuario_atual),
    db: AsyncSession = Depends(get_db),
) -> Escritorio:
    result = await db.execute(
        select(Escritorio).where(
            Escritorio.id == usuario.escritorio_id, Escritorio.ativo == True
        )
    )
    escritorio = result.scalar_one_or_none()
    if escritorio is None:
        raise HTTPException(status_code=404, detail="Escritório não encontrado")
    return escritorio


LIMITES_PLANO = {
    "free": 10,
    "starter": 100,
    "pro": 500,
    "enterprise": 999_999,
}
