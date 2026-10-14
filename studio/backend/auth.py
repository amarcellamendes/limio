from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import get_db
from .models import Usuario

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


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
    token: Optional[str] = Depends(oauth2_scheme),
    t: Optional[str] = Query(None, include_in_schema=False),  # <video src="...?t="> não manda header
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    credenciais_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciais inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = token or t
    if not token:
        raise credenciais_exc
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        usuario_id = payload.get("sub")
        if usuario_id is None:
            raise credenciais_exc
    except JWTError:
        raise credenciais_exc

    result = await db.execute(
        select(Usuario).where(Usuario.id == int(usuario_id), Usuario.ativo == True)  # noqa: E712
    )
    usuario = result.scalar_one_or_none()
    if usuario is None:
        raise credenciais_exc
    return usuario
