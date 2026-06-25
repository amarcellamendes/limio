"""
Scheduler de sincronização automática de notas recebidas.

Executa todos os dias às 06:00 e 15:00 (fuso de Manaus, UTC-4),
para todos os clientes ativos que possuem certificado A1 configurado.

O APScheduler roda dentro do processo uvicorn — sem serviço externo.
"""
import logging
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

logger = logging.getLogger("limio.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _sync_todos_clientes() -> None:
    """Sincroniza NF-e + NFS-e de todos os clientes com certificado A1."""
    from ..database import AsyncSessionLocal
    from ..models import Cliente, Escritorio

    hora = datetime.now().strftime("%H:%M")
    logger.info(f"[scheduler] Iniciando sync automático às {hora}")

    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(Cliente).where(Cliente.ativo == True)  # noqa: E712
        )
        clientes = res.scalars().all()

    total_importados = 0
    total_erros = 0

    for cliente in clientes:
        cert_path  = cliente.nfe_certificado_path  or cliente.nfse_certificado_path
        cert_senha = cliente.nfe_certificado_senha or cliente.nfse_certificado_senha

        if not cert_path or not cert_senha or not os.path.isfile(cert_path):
            continue  # Sem certificado — pula

        try:
            async with AsyncSessionLocal() as db:
                # Recarrega dentro da sessão para ter session tracking
                cli = await db.get(Cliente, cliente.id)
                if not cli:
                    continue

                # Mock do escritorio_id para o router; carregamos manualmente
                from ..models import Escritorio
                esc = await db.get(Escritorio, cli.escritorio_id)
                if not esc:
                    continue

                from ..routers.recebidos_router import _sync_nfse_recebidas
                from ..services.sefaz_dfe_service import consultar_dfe
                from ..models import DocumentoRecebido, TipoNotaEnum
                from ..routers.fornecedores_router import upsert_fornecedor
                from ..config import settings

                uf_map = {
                    "AC": "12", "AL": "27", "AP": "16", "AM": "13", "BA": "29",
                    "CE": "23", "DF": "53", "ES": "32", "GO": "52", "MA": "21",
                    "MT": "51", "MS": "50", "MG": "31", "PA": "15", "PB": "25",
                    "PR": "41", "PE": "26", "PI": "22", "RJ": "33", "RN": "24",
                    "RS": "43", "RO": "11", "RR": "14", "SC": "42", "SP": "35",
                    "SE": "28", "TO": "17",
                }
                uf_sigla = (cli.uf or "AM").upper()
                uf_code  = uf_map.get(uf_sigla, "13")
                producao = not settings.MOCK_MODE
                ultimo_nsu = cli.ultimo_nsu_nfe or "0"

                # ── NF-e SEFAZ DF-e ──────────────────────────────────────────
                resultado = await consultar_dfe(
                    cnpj=cli.cnpj,
                    uf=uf_code,
                    cert_path=cert_path,
                    cert_senha=cert_senha,
                    ultimo_nsu=ultimo_nsu,
                    producao=producao,
                )

                importados = 0
                for doc in resultado.get("documentos", []):
                    campos = doc.get("campos", {})
                    chave  = campos.get("chave_acesso") or doc.get("nsu", "")
                    if not chave:
                        continue
                    dup = await db.execute(
                        select(DocumentoRecebido.id).where(
                            DocumentoRecebido.escritorio_id == esc.id,
                            DocumentoRecebido.chave_acesso  == chave,
                        )
                    )
                    if dup.scalar_one_or_none():
                        continue

                    tipo_raw = campos.get("tipo") or doc.get("schema", "")
                    tipo = TipoNotaEnum.nfce if "65" in tipo_raw else TipoNotaEnum.nfe

                    novo = DocumentoRecebido(
                        escritorio_id=esc.id,
                        cliente_id=cli.id,
                        tipo=tipo,
                        origem="sefaz_dfe",
                        chave_acesso=chave,
                        numero=campos.get("numero"),
                        serie=campos.get("serie"),
                        data_emissao=campos.get("data_emissao"),
                        emitente_cnpj=campos.get("emitente_cnpj"),
                        emitente_razao_social=campos.get("emitente_razao_social"),
                        emitente_uf=campos.get("emitente_uf"),
                        emitente_municipio=campos.get("emitente_municipio"),
                        valor_total=campos.get("valor_total"),
                        natureza_operacao=campos.get("natureza_operacao"),
                        status_manifestacao=doc.get("status_manifestacao", "pendente"),
                        xml_content=doc.get("xml"),
                    )
                    db.add(novo)
                    await upsert_fornecedor(
                        db, esc.id,
                        cnpj=campos.get("emitente_cnpj") or "",
                        razao_social=campos.get("emitente_razao_social") or "",
                        uf=campos.get("emitente_uf"),
                        municipio=campos.get("emitente_municipio"),
                        valor_nota=campos.get("valor_total") or 0.0,
                        data_nota=campos.get("data_emissao"),
                    )
                    importados += 1

                novo_nsu = resultado.get("ultimo_nsu", ultimo_nsu)
                if novo_nsu != ultimo_nsu:
                    cli.ultimo_nsu_nfe = novo_nsu

                # ── NFS-e ─────────────────────────────────────────────────────
                nfse_importados = await _sync_nfse_recebidas(db, cli, esc, cert_path, cert_senha)
                importados += nfse_importados

                await db.commit()
                total_importados += importados

                if importados:
                    logger.info(
                        f"[scheduler] {cli.razao_social}: {importados} documento(s) importado(s)"
                    )

        except Exception as exc:
            total_erros += 1
            logger.error(f"[scheduler] Erro ao sincronizar cliente {cliente.id}: {exc}")

    logger.info(
        f"[scheduler] Sync concluído — {total_importados} documentos importados, {total_erros} erro(s)"
    )


def start_scheduler() -> AsyncIOScheduler:
    """Inicia o scheduler e registra os horários. Deve ser chamado no lifespan."""
    global _scheduler

    _scheduler = AsyncIOScheduler(timezone="America/Manaus")

    # 06:00 e 15:00 horário de Manaus (UTC-4)
    _scheduler.add_job(
        _sync_todos_clientes,
        CronTrigger(hour=6,  minute=0, timezone="America/Manaus"),
        id="sync_06h",
        name="Sync automático 06:00",
        replace_existing=True,
    )
    _scheduler.add_job(
        _sync_todos_clientes,
        CronTrigger(hour=15, minute=0, timezone="America/Manaus"),
        id="sync_15h",
        name="Sync automático 15:00",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("[scheduler] Agendamentos ativos: 06:00 e 15:00 (America/Manaus)")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Encerrado.")
