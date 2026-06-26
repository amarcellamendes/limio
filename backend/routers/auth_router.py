from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, UploadFile, File, Form
import smtplib
import asyncio
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from pydantic import BaseModel

from ..database import get_db
from ..models import Escritorio, Usuario, PlanoEnum
from ..schemas import EscritorioCreate, LoginRequest, TokenResponse, EscritorioResponse
from ..auth import (
    hash_senha, verificar_senha, criar_token,
    get_usuario_atual, get_escritorio_atual, LIMITES_PLANO,
)


class EscritorioUpdate(BaseModel):
    nome: Optional[str] = None
    email: Optional[str] = None
    telefone: Optional[str] = None
    crc: Optional[str] = None
    nota_enviar_email: Optional[bool] = None
    nota_pasta_destino: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_usuario: Optional[str] = None
    smtp_senha: Optional[str] = None

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


@router.get("/me/configuracoes")
async def get_configuracoes(
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    return {
        "nome": escritorio.nome,
        "email": escritorio.email,
        "telefone": escritorio.telefone,
        "crc": escritorio.crc,
        "nota_enviar_email": getattr(escritorio, "nota_enviar_email", False),
        "nota_pasta_destino": getattr(escritorio, "nota_pasta_destino", None),
        "smtp_host": getattr(escritorio, "smtp_host", None),
        "smtp_port": getattr(escritorio, "smtp_port", 587),
        "smtp_usuario": getattr(escritorio, "smtp_usuario", None),
        "smtp_senha_configurada": bool(getattr(escritorio, "smtp_senha", None)),
        "cert_procuracao_configurado": bool(getattr(escritorio, "cert_procuracao_path", None)),
    }


@router.put("/me/configuracoes")
async def salvar_configuracoes(
    payload: EscritorioUpdate,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    for campo, valor in payload.model_dump(exclude_none=True).items():
        setattr(escritorio, campo, valor)
    await db.commit()
    return {"ok": True}


@router.post("/me/upload-cert-procuracao")
async def upload_cert_procuracao(
    arquivo: UploadFile = File(...),
    senha: str = Form(default=""),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Upload do certificado A1 do escritório para acesso via procuração (e-CAC/eSocial)."""
    from ..config import settings
    conteudo = await arquivo.read()
    if len(conteudo) > 5 * 1024 * 1024:
        raise HTTPException(400, "Arquivo muito grande (máximo 5 MB).")
    if not arquivo.filename.lower().endswith(".pfx"):
        raise HTTPException(400, "Somente arquivos .pfx são aceitos.")

    # Valida o certificado antes de salvar
    try:
        from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12
        p12 = load_pkcs12(conteudo, senha.encode() if senha else b"")
        _ = p12.cert.certificate
    except Exception:
        raise HTTPException(400, "Certificado inválido ou senha incorreta.")

    pasta = os.path.join(settings.DATA_DIR, "certs", "escritorio")
    os.makedirs(pasta, exist_ok=True)
    nome_arquivo = f"escritorio_{escritorio.id}_procuracao.pfx"
    caminho = os.path.join(pasta, nome_arquivo)
    with open(caminho, "wb") as f:
        f.write(conteudo)

    escritorio.cert_procuracao_path = caminho
    escritorio.cert_procuracao_senha = senha
    await db.commit()
    return {"ok": True, "mensagem": "Certificado de procuração salvo com sucesso."}


def _enviar_email_sync(host: str, port: int, usuario: str, senha: str,
                        destinatario: str, assunto: str, corpo: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = usuario
    msg["To"] = destinatario
    msg.attach(MIMEText(corpo, "html"))
    with smtplib.SMTP(host, port, timeout=10) as s:
        s.starttls()
        s.login(usuario, senha)
        s.sendmail(usuario, destinatario, msg.as_string())


async def enviar_email(host: str, port: int, usuario: str, senha: str,
                       destinatario: str, assunto: str, corpo: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, _enviar_email_sync, host, port, usuario, senha, destinatario, assunto, corpo
    )


@router.post("/me/testar-smtp")
async def testar_smtp(
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    host = getattr(escritorio, "smtp_host", None)
    porta = getattr(escritorio, "smtp_port", 587) or 587
    usuario = getattr(escritorio, "smtp_usuario", None)
    senha = getattr(escritorio, "smtp_senha", None)

    if not all([host, usuario, senha]):
        raise HTTPException(400, "Configure o servidor SMTP antes de testar.")

    try:
        await enviar_email(
            host=host, port=porta, usuario=usuario, senha=senha,
            destinatario=usuario,
            assunto=f"Limio — Teste de configuração SMTP de {escritorio.nome}",
            corpo=f"<p>Olá! Este é um e-mail de teste enviado pelo <strong>Limio</strong>.</p>"
                  f"<p>As configurações SMTP do escritório <strong>{escritorio.nome}</strong> estão funcionando corretamente.</p>",
        )
    except Exception as e:
        raise HTTPException(500, f"Erro ao enviar e-mail: {str(e)}")

    return {"ok": True}
