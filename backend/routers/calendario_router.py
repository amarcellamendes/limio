from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from datetime import date

from ..database import get_db
from ..models import Cliente, Escritorio
from ..auth import get_escritorio_atual
from ..services.calendario_service import gerar_eventos, proximos_vencimentos

router = APIRouter(prefix="/api/calendario", tags=["Calendário Fiscal"])


def _clientes_para_dict(clientes):
    def _fmt(val):
        if val is None:
            return None
        return val.date().isoformat() if hasattr(val, "date") else str(val)[:10]

    return [
        {
            "id": c.id,
            "razao_social": c.razao_social,
            "optante_simples": c.optante_simples,
            "regime_tributario": c.regime_tributario,
            "uf": c.uf,
            "municipio": c.municipio,
            "codigo_ibge": c.codigo_ibge,
            "nfse_certificado_path": getattr(c, "nfse_certificado_path", None),
            "nfe_certificado_path":  getattr(c, "nfe_certificado_path", None),
            "nfse_certificado_vencimento": _fmt(getattr(c, "nfse_certificado_vencimento", None)),
            "nfe_certificado_vencimento":  _fmt(getattr(c, "nfe_certificado_vencimento", None)),
        }
        for c in clientes
    ]


@router.get("/mes")
async def calendario_mes(
    ano: int = Query(default=date.today().year),
    mes: int = Query(default=date.today().month),
    cliente_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    q = select(Cliente).where(Cliente.escritorio_id == escritorio.id, Cliente.ativo == True)
    if cliente_id:
        q = q.where(Cliente.id == cliente_id)
    result = await db.execute(q)
    clientes = result.scalars().all()
    return gerar_eventos(ano, mes, _clientes_para_dict(clientes))


@router.get("/proximos")
async def proximos(
    dias: int = Query(default=30, ge=1, le=90),
    cliente_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    q = select(Cliente).where(Cliente.escritorio_id == escritorio.id, Cliente.ativo == True)
    if cliente_id:
        q = q.where(Cliente.id == cliente_id)
    result = await db.execute(q)
    clientes = result.scalars().all()
    return proximos_vencimentos(_clientes_para_dict(clientes), dias=dias)
