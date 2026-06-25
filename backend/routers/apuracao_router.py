"""
Apuração de tributos: Simples Nacional (Anexos I-V), Fator R, folha de pagamento.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel

from ..database import get_db
from ..models import Nota, Cliente, Escritorio, StatusNotaEnum, FolhaMensal, ReceitaHistorica, IcmsMensal
from ..auth import get_escritorio_atual

router = APIRouter(tags=["apuracao"])


# ---------------------------------------------------------------------------
# Tabelas do Simples Nacional 2024 (LC 123/2006 — Resolução CGSN 140/2018)
# Formato: (receita_min, receita_max, aliq_nominal, parcela_deduzir)
# ---------------------------------------------------------------------------
TABELAS_SIMPLES = {
    "I": {
        "nome": "Anexo I — Comércio",
        "cpp_no_das": True,
        "faixas": [
            (0,          180_000,     0.0400,  0.00),
            (180_000,    360_000,     0.0730,  5_940.00),
            (360_000,    720_000,     0.0950,  13_860.00),
            (720_000,    1_800_000,   0.1070,  22_500.00),
            (1_800_000,  3_600_000,   0.1430,  87_300.00),
            (3_600_000,  4_800_000,   0.1900,  378_000.00),
        ],
    },
    "II": {
        "nome": "Anexo II — Indústria",
        "cpp_no_das": True,
        "faixas": [
            (0,          180_000,     0.0450,  0.00),
            (180_000,    360_000,     0.0780,  5_940.00),
            (360_000,    720_000,     0.1000,  13_860.00),
            (720_000,    1_800_000,   0.1120,  22_500.00),
            (1_800_000,  3_600_000,   0.1470,  85_500.00),
            (3_600_000,  4_800_000,   0.3000,  720_000.00),
        ],
    },
    "III": {
        "nome": "Anexo III — Serviços (CPP inclusa · ISS reduzido)",
        "cpp_no_das": True,
        "faixas": [
            (0,          180_000,     0.0600,  0.00),
            (180_000,    360_000,     0.1120,  9_360.00),
            (360_000,    720_000,     0.1320,  17_640.00),
            (720_000,    1_800_000,   0.1600,  35_640.00),
            (1_800_000,  3_600_000,   0.2100,  125_640.00),
            (3_600_000,  4_800_000,   0.3300,  648_000.00),
        ],
    },
    "IV": {
        "nome": "Anexo IV — Serviços (CPP FORA do DAS · INSS separado)",
        "cpp_no_das": False,
        "faixas": [
            (0,          180_000,     0.0450,  0.00),
            (180_000,    360_000,     0.0900,  8_100.00),
            (360_000,    720_000,     0.1020,  12_420.00),
            (720_000,    1_800_000,   0.1400,  39_780.00),
            (1_800_000,  3_600_000,   0.2200,  183_780.00),
            (3_600_000,  4_800_000,   0.3300,  828_000.00),
        ],
    },
    "V": {
        "nome": "Anexo V — Serviços (CPP inclusa · alíquota maior sem Fator R)",
        "cpp_no_das": True,
        "faixas": [
            (0,          180_000,     0.1550,  0.00),
            (180_000,    360_000,     0.1800,  4_500.00),
            (360_000,    720_000,     0.1950,  9_900.00),
            (720_000,    1_800_000,   0.2050,  17_100.00),
            (1_800_000,  3_600_000,   0.2300,  62_100.00),
            (3_600_000,  4_800_000,   0.3050,  540_000.00),
        ],
    },
}

DARF_CODIGOS = {
    "pis":    {"codigo": "6912", "descricao": "PIS/Pasep — Retenção sobre serviços",             "venc": "dia 25 do mês seguinte"},
    "cofins": {"codigo": "5856", "descricao": "COFINS — Retenção sobre serviços",                "venc": "dia 25 do mês seguinte"},
    "inss":   {"codigo": "2240", "descricao": "INSS — Contribuição Retida (cessão mão de obra)", "venc": "dia 20 do mês seguinte"},
    "ir":     {"codigo": "0588", "descricao": "IRRF — Rendimentos do Trabalho / PJ",             "venc": "dia 20 do mês seguinte"},
    "csll":   {"codigo": "3047", "descricao": "CSLL — Retenção na Fonte",                        "venc": "dia 20 do mês seguinte"},
    "das":    {"codigo": "DAS",  "descricao": "DAS — Documento de Arrecadação Simples",          "venc": "dia 20 do mês seguinte"},
    "icms":   {"codigo": "ICMS", "descricao": "ICMS — Mercadorias (Simples: incluso no DAS)",    "venc": "varia por estado — consulte SEFAZ estadual"},
    "gps":    {"codigo": "GPS",  "descricao": "GPS — INSS Patronal (Anexo IV fora do DAS)",      "venc": "dia 20 do mês seguinte"},
}


def _calcular_aliquota_efetiva(rbt12: float, anexo: str) -> dict:
    """Retorna alíquota efetiva, nominal, parcela e faixa para um RBT12 dado."""
    if anexo not in TABELAS_SIMPLES or rbt12 <= 0:
        return {"faixa": None, "aliq_nominal": 0.0, "parcela_deduzir": 0.0,
                "aliq_efetiva": 0.0, "acima_limite": rbt12 > 4_800_000}

    for i, (fmin, fmax, aliq_nom, pd) in enumerate(TABELAS_SIMPLES[anexo]["faixas"], 1):
        if rbt12 <= fmax or i == len(TABELAS_SIMPLES[anexo]["faixas"]):
            aliq_ef = round((rbt12 * aliq_nom - pd) / rbt12, 6)
            return {
                "faixa": i,
                "faixa_descricao": f"Faixa {i}: até R$ {fmax:,.2f}",
                "aliq_nominal": aliq_nom,
                "parcela_deduzir": pd,
                "aliq_efetiva": aliq_ef,
                "acima_limite": False,
            }


def _anexo_efetivo(cliente: Cliente, fator_r: float) -> str:
    """Determina qual Anexo se aplica na prática, considerando o Fator R."""
    base = cliente.anexo_simples or "III"
    if base == "III_V":
        return "III" if fator_r >= 0.28 else "V"
    return base if base in TABELAS_SIMPLES else "III"


# ---------------------------------------------------------------------------
# Schemas inline para folha
# ---------------------------------------------------------------------------
class FolhaInput(BaseModel):
    valor_salarios: float = 0.0
    valor_pro_labore: float = 0.0
    valor_inss_patronal: float = 0.0
    valor_fgts: float = 0.0
    observacao: Optional[str] = None


class ReceitaHistoricaInput(BaseModel):
    valor_receita: float
    origem: str = "pgdas_d"  # pgdas_d / manual


class IcmsInput(BaseModel):
    credito: float = 0.0
    debito: float = 0.0
    observacao: Optional[str] = None
    origem: str = "manual"


# ---------------------------------------------------------------------------
# Endpoints de folha de pagamento
# ---------------------------------------------------------------------------

@router.get("/api/folha/{cliente_id}")
async def listar_folha(
    cliente_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Lista os registros de folha dos últimos 24 meses para um cliente."""
    r = await db.execute(
        select(FolhaMensal)
        .where(FolhaMensal.cliente_id == cliente_id,
               FolhaMensal.escritorio_id == escritorio.id)
        .order_by(FolhaMensal.competencia.desc())
        .limit(24)
    )
    registros = r.scalars().all()
    return [
        {
            "id": f.id,
            "competencia": f.competencia,
            "valor_salarios": f.valor_salarios,
            "valor_pro_labore": f.valor_pro_labore,
            "valor_inss_patronal": f.valor_inss_patronal,
            "valor_fgts": f.valor_fgts,
            "valor_total": f.valor_total,
            "origem": f.origem,
            "observacao": f.observacao,
        }
        for f in registros
    ]


@router.post("/api/folha/{cliente_id}/{competencia}")
async def salvar_folha(
    cliente_id: int,
    competencia: str,
    body: FolhaInput,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Cria ou atualiza o registro de folha de um mês (upsert)."""
    r = await db.execute(
        select(FolhaMensal).where(
            FolhaMensal.cliente_id == cliente_id,
            FolhaMensal.escritorio_id == escritorio.id,
            FolhaMensal.competencia == competencia,
        )
    )
    folha = r.scalar_one_or_none()
    total = body.valor_salarios + body.valor_pro_labore + body.valor_inss_patronal + body.valor_fgts

    if folha:
        folha.valor_salarios = body.valor_salarios
        folha.valor_pro_labore = body.valor_pro_labore
        folha.valor_inss_patronal = body.valor_inss_patronal
        folha.valor_fgts = body.valor_fgts
        folha.valor_total = total
        folha.observacao = body.observacao
        folha.atualizado_em = datetime.utcnow()
    else:
        folha = FolhaMensal(
            escritorio_id=escritorio.id,
            cliente_id=cliente_id,
            competencia=competencia,
            valor_salarios=body.valor_salarios,
            valor_pro_labore=body.valor_pro_labore,
            valor_inss_patronal=body.valor_inss_patronal,
            valor_fgts=body.valor_fgts,
            valor_total=total,
            observacao=body.observacao,
        )
        db.add(folha)
    await db.commit()
    return {"ok": True, "competencia": competencia, "valor_total": total}


@router.delete("/api/folha/{cliente_id}/{competencia}")
async def excluir_folha(
    cliente_id: int,
    competencia: str,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(FolhaMensal).where(
            FolhaMensal.cliente_id == cliente_id,
            FolhaMensal.escritorio_id == escritorio.id,
            FolhaMensal.competencia == competencia,
        )
    )
    folha = r.scalar_one_or_none()
    if not folha:
        raise HTTPException(404, "Registro não encontrado.")
    await db.delete(folha)
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Endpoints de receita histórica
# ---------------------------------------------------------------------------

@router.get("/api/receita-historica/{cliente_id}")
async def listar_receita_historica(
    cliente_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(ReceitaHistorica)
        .where(ReceitaHistorica.cliente_id == cliente_id,
               ReceitaHistorica.escritorio_id == escritorio.id)
        .order_by(ReceitaHistorica.competencia.desc())
        .limit(36)
    )
    return [{"id": h.id, "competencia": h.competencia, "valor_receita": h.valor_receita, "origem": h.origem}
            for h in r.scalars().all()]


@router.post("/api/receita-historica/{cliente_id}/{competencia}")
async def salvar_receita_historica(
    cliente_id: int,
    competencia: str,
    body: ReceitaHistoricaInput,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(ReceitaHistorica).where(
            ReceitaHistorica.cliente_id == cliente_id,
            ReceitaHistorica.escritorio_id == escritorio.id,
            ReceitaHistorica.competencia == competencia,
        )
    )
    rec = r.scalar_one_or_none()
    if rec:
        rec.valor_receita = body.valor_receita
        rec.origem = body.origem
        rec.atualizado_em = datetime.utcnow()
    else:
        rec = ReceitaHistorica(
            escritorio_id=escritorio.id, cliente_id=cliente_id,
            competencia=competencia, valor_receita=body.valor_receita, origem=body.origem,
        )
        db.add(rec)
    await db.commit()
    return {"ok": True, "competencia": competencia, "valor_receita": body.valor_receita}


@router.delete("/api/receita-historica/{cliente_id}/{competencia}")
async def excluir_receita_historica(
    cliente_id: int,
    competencia: str,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(ReceitaHistorica).where(
            ReceitaHistorica.cliente_id == cliente_id,
            ReceitaHistorica.escritorio_id == escritorio.id,
            ReceitaHistorica.competencia == competencia,
        )
    )
    rec = r.scalar_one_or_none()
    if not rec:
        raise HTTPException(404, "Registro não encontrado.")
    await db.delete(rec)
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Endpoint principal de apuração
# ---------------------------------------------------------------------------

@router.get("/api/apuracao/{cliente_id}/{ano}")
async def apuracao_anual(
    cliente_id: int,
    ano: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Apuração anual com Simples Nacional (Anexos I-V), Fator R e totais por tributo."""
    cli_r = await db.execute(
        select(Cliente).where(Cliente.id == cliente_id, Cliente.escritorio_id == escritorio.id)
    )
    cliente = cli_r.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")

    # ── Notas emitidas no ano ────────────────────────────────────────────────
    q_notas = await db.execute(
        select(Nota).where(
            Nota.escritorio_id == escritorio.id,
            Nota.cliente_id == cliente_id,
            Nota.status == StatusNotaEnum.emitida,
            Nota.data_competencia.like(f"{ano}-%"),
        )
    )
    notas = q_notas.scalars().all()

    # ── Folha e receita histórica: buscamos 12 meses antes do ano ───────────
    rbt12_inicio = f"{ano-1}-01"
    q_notas_rbt12 = await db.execute(
        select(Nota).where(
            Nota.escritorio_id == escritorio.id,
            Nota.cliente_id == cliente_id,
            Nota.status == StatusNotaEnum.emitida,
            Nota.data_competencia >= rbt12_inicio,
            Nota.data_competencia < f"{ano+1}-01",
        )
    )
    todas_notas_rbt = q_notas_rbt12.scalars().all()

    q_folha = await db.execute(
        select(FolhaMensal).where(
            FolhaMensal.escritorio_id == escritorio.id,
            FolhaMensal.cliente_id == cliente_id,
            FolhaMensal.competencia >= rbt12_inicio,
        ).order_by(FolhaMensal.competencia)
    )
    todas_folhas = {f.competencia: f.valor_total for f in q_folha.scalars().all()}

    # Receita histórica lançada manualmente (PGDAS-D ou manual)
    q_hist = await db.execute(
        select(ReceitaHistorica).where(
            ReceitaHistorica.escritorio_id == escritorio.id,
            ReceitaHistorica.cliente_id == cliente_id,
            ReceitaHistorica.competencia >= rbt12_inicio,
        )
    )
    receita_historica_por_mes = {h.competencia: (h.valor_receita, h.origem)
                                  for h in q_hist.scalars().all()}

    # ICMS: busca crédito/débito por mês do ano
    q_icms = await db.execute(
        select(IcmsMensal).where(
            IcmsMensal.escritorio_id == escritorio.id,
            IcmsMensal.cliente_id == cliente_id,
            IcmsMensal.competencia >= f"{ano}-01",
            IcmsMensal.competencia <= f"{ano}-12",
        )
    )
    icms_por_mes = {i.competencia: {"credito": i.credito, "debito": i.debito, "saldo": i.saldo,
                                     "observacao": i.observacao}
                   for i in q_icms.scalars().all()}

    # ── Agrupa receita por mês: notas reais têm prioridade sobre histórico ──
    receita_por_mes: dict[str, float] = {}
    meses_com_notas: set[str] = set()
    for n in todas_notas_rbt:
        mes = (n.data_competencia or "0000-00")[:7]
        receita_por_mes[mes] = receita_por_mes.get(mes, 0.0) + (n.valor_servico or 0)
        meses_com_notas.add(mes)
    # Para meses sem notas, usa receita histórica (se houver)
    for mes, (valor, _) in receita_historica_por_mes.items():
        if mes not in meses_com_notas:
            receita_por_mes[mes] = receita_por_mes.get(mes, 0.0) + valor

    tributos_por_mes: dict[str, dict] = {}
    for n in notas:
        mes = (n.data_competencia or "0000-00")[:7]
        if mes not in tributos_por_mes:
            tributos_por_mes[mes] = {
                "competencia": mes, "qtd_notas": 0, "receita_bruta": 0.0,
                "iss": 0.0, "pis": 0.0, "cofins": 0.0,
                "inss": 0.0, "ir": 0.0, "csll": 0.0,
            }
        m = tributos_por_mes[mes]
        m["qtd_notas"] += 1
        m["receita_bruta"] += n.valor_servico or 0
        m["iss"]    += n.valor_iss    or 0
        m["pis"]    += n.valor_pis    or 0
        m["cofins"] += n.valor_cofins or 0
        m["inss"]   += n.valor_inss   or 0
        m["ir"]     += n.valor_ir     or 0
        m["csll"]   += n.valor_csll   or 0

    # ── Por mês: calcula RBT12, Folha12m, Fator R, DAS ─────────────────────
    is_simples = cliente.optante_simples
    permite_fator_r = cliente.atividade_permite_fator_r

    meses_lista = []
    for mes in sorted(tributos_por_mes.keys()):
        m = tributos_por_mes[mes]

        # RBT12: receita dos 12 meses terminando neste mês (inclusive)
        year_m, month_m = int(mes[:4]), int(mes[5:7])
        meses_janela = []
        for delta in range(12):
            mm = month_m - delta
            yy = year_m
            while mm <= 0:
                mm += 12
                yy -= 1
            meses_janela.append(f"{yy:04d}-{mm:02d}")
        rbt12 = round(sum(receita_por_mes.get(k, 0.0) for k in meses_janela), 2)
        meses_sem_dados = [k for k in meses_janela if k not in receita_por_mes]

        # Folha 12m para Fator R
        folha12m = round(sum(todas_folhas.get(k, 0.0) for k in meses_janela), 2)
        fator_r = round(folha12m / rbt12, 4) if rbt12 > 0 else 0.0

        # Anexo efetivo e alíquota do DAS
        anexo = _anexo_efetivo(cliente, fator_r) if is_simples else None
        das_info = _calcular_aliquota_efetiva(rbt12, anexo) if anexo else None
        valor_das = round(m["receita_bruta"] * (das_info["aliq_efetiva"] if das_info else 0), 2)

        icms_mes = icms_por_mes.get(mes, {})
        icms_credito = icms_mes.get("credito", 0.0)
        icms_debito  = icms_mes.get("debito", 0.0)
        icms_saldo   = icms_mes.get("saldo", round(icms_debito - icms_credito, 2))

        m.update({
            "rbt12": rbt12,
            "folha_mes": todas_folhas.get(mes, 0.0),
            "folha12m": folha12m,
            "fator_r": fator_r,
            "fator_r_pct": round(fator_r * 100, 2),
            "fator_r_atingido": fator_r >= 0.28 if permite_fator_r else None,
            "anexo_aplicado": anexo,
            "aliq_efetiva": das_info["aliq_efetiva"] if das_info else 0,
            "aliq_nominal": das_info["aliq_nominal"] if das_info else 0,
            "faixa_simples": das_info["faixa"] if das_info else None,
            "valor_das": valor_das,
            "cpp_no_das": TABELAS_SIMPLES.get(anexo, {}).get("cpp_no_das", True) if anexo else True,
            "meses_sem_dados_rbt12": len(meses_sem_dados),
            "alerta_rbt12_incompleto": len(meses_sem_dados) > 0,
            "icms_credito": icms_credito,
            "icms_debito": icms_debito,
            "icms_saldo": icms_saldo,
            "icms_observacao": icms_mes.get("observacao"),
            "total_tributos": round(
                m["iss"] + m["pis"] + m["cofins"] + m["inss"] + m["ir"] + m["csll"], 2
            ),
        })
        for k in ["receita_bruta", "iss", "pis", "cofins", "inss", "ir", "csll"]:
            m[k] = round(m[k], 2)
        meses_lista.append(m)

    # ── Totais anuais ────────────────────────────────────────────────────────
    totais = {k: round(sum(m[k] for m in meses_lista), 2)
              for k in ["receita_bruta", "iss", "pis", "cofins", "inss", "ir", "csll",
                        "valor_das", "icms_credito", "icms_debito", "icms_saldo"]}
    totais["total_tributos"] = round(
        sum(totais[k] for k in ["iss", "pis", "cofins", "inss", "ir", "csll"]), 2
    )

    # ── Fator R médio do ano ─────────────────────────────────────────────────
    receita_ano = totais["receita_bruta"]
    folha_ano = round(sum(todas_folhas.get(f"{ano}-{m:02d}", 0.0) for m in range(1, 13)), 2)
    fator_r_medio = round(folha_ano / receita_ano, 4) if receita_ano > 0 else 0.0
    pct_simples = round((receita_ano / cliente.limite_simples) * 100, 1) if cliente.limite_simples else 0

    # ── Análise Fator R ──────────────────────────────────────────────────────
    analise_fator_r = None
    if is_simples and permite_fator_r and cliente.anexo_simples == "III_V":
        rbt12_atual = round(sum(receita_por_mes.get(f"{ano}-{m:02d}", 0.0) for m in range(1, 13)), 2)
        info_iii = _calcular_aliquota_efetiva(rbt12_atual, "III")
        info_v   = _calcular_aliquota_efetiva(rbt12_atual, "V")
        analise_fator_r = {
            "fator_r_medio_ano": fator_r_medio,
            "fator_r_pct": round(fator_r_medio * 100, 2),
            "atingiu_fator_r": fator_r_medio >= 0.28,
            "folha_ano": folha_ano,
            "receita_ano": receita_ano,
            "folha_minima_para_fator_r": round(receita_ano * 0.28, 2),
            "diferenca_para_fator_r": round(receita_ano * 0.28 - folha_ano, 2),
            "anexo_atual": "III" if fator_r_medio >= 0.28 else "V",
            "aliq_efetiva_iii": info_iii["aliq_efetiva"] if info_iii else 0,
            "aliq_efetiva_v": info_v["aliq_efetiva"] if info_v else 0,
            "economia_mensal_estimada": round(
                (receita_ano / 12) * abs((info_v["aliq_efetiva"] or 0) - (info_iii["aliq_efetiva"] or 0)), 2
            ) if info_iii and info_v else 0,
        }

    # ── Tabelas de Anexos disponíveis para referência ────────────────────────
    tabela_referencia = {
        k: {
            "nome": v["nome"],
            "cpp_no_das": v["cpp_no_das"],
            "faixas": [
                {"faixa": i+1, "ate": f[1], "aliq_nominal": f[2], "parcela_deduzir": f[3]}
                for i, f in enumerate(v["faixas"])
            ],
        }
        for k, v in TABELAS_SIMPLES.items()
    }

    return {
        "cliente_id": cliente_id,
        "razao_social": cliente.razao_social,
        "ano": ano,
        "optante_simples": is_simples,
        "anexo_simples": cliente.anexo_simples,
        "atividade_permite_fator_r": permite_fator_r,
        "limite_simples": cliente.limite_simples,
        "receita_ano": receita_ano,
        "pct_limite_simples": pct_simples,
        "alerta_simples": pct_simples >= 80,
        "folha_ano": folha_ano,
        "analise_fator_r": analise_fator_r,
        "meses": meses_lista,
        "totais": totais,
        "darf_codigos": DARF_CODIGOS,
        "tabela_referencia": tabela_referencia,
        "receita_historica": {
            mes: {"valor": valor, "origem": origem}
            for mes, (valor, origem) in receita_historica_por_mes.items()
        },
        "meses_com_notas_limio": sorted(meses_com_notas),
        "observacoes": [
            obs for obs in [
                "ISS: recolher via DAM na prefeitura do município prestador." if totais["iss"] > 0 else None,
                "DAS: recolher via PGDAS-D até o dia 20 do mês seguinte." if is_simples and totais["valor_das"] > 0 else None,
                "Anexo IV: INSS patronal (CPP) é recolhido separadamente via GPS/eSocial — não incluso no DAS." if is_simples and cliente.anexo_simples == "IV" else None,
                "INSS retido: recolher via GPS/eSocial até o dia 20 do mês seguinte." if totais["inss"] > 0 else None,
                "Empresa não optante pelo Simples: PIS/COFINS/IRPJ/CSLL recolhidos por DARFs separados." if not is_simples and (totais["pis"] + totais["cofins"]) > 0 else None,
            ]
            if obs is not None
        ],
    }


# ---------------------------------------------------------------------------
# Status rápido do Simples (para lista de clientes)
# ---------------------------------------------------------------------------

@router.get("/api/apuracao/simples/{cliente_id}")
async def status_simples(
    cliente_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    cli_r = await db.execute(
        select(Cliente).where(Cliente.id == cliente_id, Cliente.escritorio_id == escritorio.id)
    )
    cliente = cli_r.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")

    ano = date.today().year
    r = await db.execute(
        select(func.sum(Nota.valor_servico)).where(
            Nota.escritorio_id == escritorio.id,
            Nota.cliente_id == cliente_id,
            Nota.status == StatusNotaEnum.emitida,
            Nota.data_competencia.like(f"{ano}-%"),
        )
    )
    receita = r.scalar() or 0.0
    pct = round((receita / cliente.limite_simples) * 100, 1) if cliente.limite_simples else 0
    return {
        "receita_ano": round(receita, 2),
        "limite": cliente.limite_simples,
        "pct": pct,
        "alerta": pct >= 80,
        "perigo": pct >= 95,
    }


# ---------------------------------------------------------------------------
# Ranking Simples Nacional (para dashboard)
# ---------------------------------------------------------------------------

@router.get("/api/apuracao/ranking-simples")
async def ranking_simples(
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """Retorna todos os clientes Simples ordenados pelo % do limite anual atingido."""
    clientes_r = await db.execute(
        select(Cliente).where(
            Cliente.escritorio_id == escritorio.id,
            Cliente.ativo == True,
            Cliente.optante_simples == True,
        ).order_by(Cliente.razao_social)
    )
    clientes = clientes_r.scalars().all()

    ano = date.today().year
    ranking = []
    for c in clientes:
        r = await db.execute(
            select(func.sum(Nota.valor_servico)).where(
                Nota.escritorio_id == escritorio.id,
                Nota.cliente_id == c.id,
                Nota.status == StatusNotaEnum.emitida,
                Nota.data_competencia.like(f"{ano}-%"),
            )
        )
        receita = r.scalar() or 0.0
        limite = c.limite_simples or 4_800_000.0
        pct = round(min(receita / limite * 100, 100), 1) if limite > 0 else 0.0
        ranking.append({
            "cliente_id": c.id,
            "razao_social": c.razao_social,
            "nome_fantasia": c.nome_fantasia,
            "receita_ano": round(receita, 2),
            "limite": limite,
            "pct": pct,
            "alerta": pct >= 70,
            "perigo": pct >= 90,
            "anexo": c.anexo_simples,
        })

    ranking.sort(key=lambda x: x["pct"], reverse=True)
    return ranking


# ---------------------------------------------------------------------------
# ICMS Mensal — crédito / débito por competência
# ---------------------------------------------------------------------------

@router.get("/api/icms/{cliente_id}")
async def listar_icms(
    cliente_id: int,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(IcmsMensal)
        .where(IcmsMensal.cliente_id == cliente_id, IcmsMensal.escritorio_id == escritorio.id)
        .order_by(IcmsMensal.competencia.desc()).limit(36)
    )
    return [{"id": i.id, "competencia": i.competencia, "credito": i.credito,
             "debito": i.debito, "saldo": i.saldo, "observacao": i.observacao}
            for i in r.scalars().all()]


@router.post("/api/icms/{cliente_id}/{competencia}")
async def salvar_icms(
    cliente_id: int,
    competencia: str,
    body: IcmsInput,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(IcmsMensal).where(
            IcmsMensal.cliente_id == cliente_id,
            IcmsMensal.escritorio_id == escritorio.id,
            IcmsMensal.competencia == competencia,
        )
    )
    icms = r.scalar_one_or_none()
    saldo = round(body.debito - body.credito, 2)
    if icms:
        icms.credito = body.credito
        icms.debito = body.debito
        icms.saldo = saldo
        icms.observacao = body.observacao
        icms.origem = body.origem
        icms.atualizado_em = datetime.utcnow()
    else:
        icms = IcmsMensal(
            escritorio_id=escritorio.id, cliente_id=cliente_id,
            competencia=competencia, credito=body.credito,
            debito=body.debito, saldo=saldo,
            observacao=body.observacao, origem=body.origem,
        )
        db.add(icms)
    await db.commit()
    return {"ok": True, "competencia": competencia, "saldo": saldo}


@router.delete("/api/icms/{cliente_id}/{competencia}")
async def excluir_icms(
    cliente_id: int,
    competencia: str,
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(
        select(IcmsMensal).where(
            IcmsMensal.cliente_id == cliente_id,
            IcmsMensal.escritorio_id == escritorio.id,
            IcmsMensal.competencia == competencia,
        )
    )
    icms = r.scalar_one_or_none()
    if not icms:
        raise HTTPException(404, "Registro não encontrado.")
    await db.delete(icms)
    await db.commit()
    return {"ok": True}
