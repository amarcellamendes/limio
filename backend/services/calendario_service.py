"""
Calendário Fiscal Brasileiro — 2025–2028
Cobre: Receita Federal, SEFAZ de todos os estados (ICMS) e ISS dos principais municípios.

Regras de prorrogação: quando o vencimento cai em sábado, domingo ou feriado nacional,
o prazo é prorrogado para o próximo dia útil (regra geral RFB / CONFAZ).
"""
from datetime import date, timedelta
from typing import List, Dict, Any, Optional
import calendar as _cal

# ─── Feriados nacionais ────────────────────────────────────────────────────

_FIXOS = [  # (dia, mês)
    (1, 1), (21, 4), (1, 5), (7, 9), (12, 10), (2, 11), (15, 11), (20, 11), (25, 12)
]

_MOVEIS: Dict[int, List[date]] = {
    # Carnaval Seg/Ter, Sexta-feira Santa, Corpus Christi
    2025: [date(2025,3,3),  date(2025,3,4),  date(2025,4,18), date(2025,6,19)],
    2026: [date(2026,2,16), date(2026,2,17), date(2026,4,3),  date(2026,6,4)],
    2027: [date(2027,2,8),  date(2027,2,9),  date(2027,3,26), date(2027,5,27)],
    2028: [date(2028,2,28), date(2028,2,29), date(2028,4,14), date(2028,6,15)],
}

def _feriados(ano: int) -> set:
    s = set(_MOVEIS.get(ano, []))
    for dia, mes in _FIXOS:
        try:
            s.add(date(ano, mes, dia))
        except ValueError:
            pass
    return s


def _pdu(d: date, feriados: set) -> date:
    """Próximo dia útil a partir de d (inclusive)."""
    while d.weekday() >= 5 or d in feriados:
        d += timedelta(1)
    return d


def _udu(ano: int, mes: int, feriados: set) -> date:
    """Último dia útil do mês."""
    d = date(ano, mes, _cal.monthrange(ano, mes)[1])
    while d.weekday() >= 5 or d in feriados:
        d -= timedelta(1)
    return d


def _dia(ano: int, mes: int, dia: int, feriados: set) -> date:
    """Vencimento no dia alvo (ou próximo dia útil se cair em fim de semana/feriado)."""
    ultimo = _cal.monthrange(ano, mes)[1]
    d = date(ano, mes, min(dia, ultimo))
    return _pdu(d, feriados)


# ─── Vencimentos ICMS por UF ───────────────────────────────────────────────
# Dia do mês (mês de competência + 1). Fonte: regulamentos estaduais vigentes.
# Empresas no Simples Nacional recolhem ICMS dentro do DAS — não geram evento separado.

ICMS_DIA_POR_UF: Dict[str, int] = {
    "AC": 9,   # RICMS/AC art. 143
    "AL": 9,   # RICMS/AL — comércio/indústria geral
    "AM": 9,   # RICMS/AM Decreto 20.686/99
    "AP": 9,   # RICMS/AP
    "BA": 9,   # RICMS/BA
    "CE": 9,   # SEFAZ CE — GNR DIA 9
    "DF": 10,  # RICMS/DF art. 74
    "ES": 9,   # RICMS/ES
    "GO": 10,  # SEFAZ GO — GIA/ICMS dia 10
    "MA": 9,   # RICMS/MA
    "MT": 6,   # SEFAZ MT — regime normal dia 6 (Portaria 130/2008)
    "MS": 9,   # RICMS/MS
    "MG": 15,  # SEFAZ MG — comércio/indústria geral dia 15
    "PA": 9,   # RICMS/PA
    "PB": 9,   # RICMS/PB
    "PR": 18,  # SEFAZ PR — comércio/indústria dia 18 (varia por grupo CNAE)
    "PE": 10,  # SEFAZ PE — GNRE dia 10
    "PI": 9,   # RICMS/PI
    "RJ": 20,  # SEFAZ RJ — ICMS GIA dia 20
    "RN": 9,   # SEFAZ RN — GIA dia 9
    "RS": 25,  # SEFAZ RS — GIA-ICMS dia 25
    "RO": 9,   # RICMS/RO
    "RR": 9,   # RICMS/RR
    "SC": 25,  # SEFAZ SC — DIME dia 25
    "SP": 20,  # SEFAZ SP — GIA/ICMS dia 20 (segmento A; varia por setor)
    "SE": 9,   # SEFAZ SE
    "TO": 9,   # SEFAZ TO
}

# ─── Vencimentos ISS por código IBGE ──────────────────────────────────────
# Dia do mês SEGUINTE à competência. Padrão = dia 10.
# Exceções mapeadas abaixo (legislação municipal).

_ISS_EXCECOES: Dict[str, int] = {
    # Vitória/ES — dia 5 (Lei Municipal 6.077/2003)
    "3205309": 5,
    # Campinas/SP — dia 5 (Dec. Municipal 15.356/2005)
    "3509502": 5,
    # Santos/SP — dia 5
    "3548500": 5,
    # Guarulhos/SP — dia 10 (padrão, listado explicitamente)
    "3518800": 10,
    # São Bernardo do Campo/SP — dia 10
    "3548708": 10,
}
_ISS_PADRAO = 10


def iss_dia(codigo_ibge: Optional[str]) -> int:
    if codigo_ibge and str(codigo_ibge) in _ISS_EXCECOES:
        return _ISS_EXCECOES[str(codigo_ibge)]
    return _ISS_PADRAO


# ─── Cores por categoria ──────────────────────────────────────────────────

CORES = {
    "federal":       "#013957",
    "previdenciario":"#d88d2a",
    "estadual":      "#16a34a",
    "municipal":     "#7c3aed",
    "acessoria":     "#64748b",
    "certificado":   "#dc2626",  # vermelho
}


# ─── Gerador principal ────────────────────────────────────────────────────

def gerar_eventos(
    ano: int,
    mes: int,
    clientes: List[Dict],
) -> List[Dict[str, Any]]:
    """
    Retorna lista de eventos fiscais para o mês/ano dado,
    considerando os regimes, UFs e municípios dos clientes fornecidos.

    Cada evento:
      {
        "data": "2026-06-20",
        "descricao": str,
        "detalhes": str,
        "categoria": "federal"|"estadual"|"municipal"|"previdenciario"|"acessoria",
        "cor": str,
        "clientes": [{"id": int, "nome": str}],
        "regime_alvo": ["simples"|"presumido"|"real"|"todos"],
      }
    """
    feriados = _feriados(ano)
    # Mês de vencimento: geralmente mês seguinte ao mês de competência
    # O calendário exibe os eventos que VENCEM neste mês, não a competência.
    # Portanto mês de competência = mes-1 (ou 12 se mes=1).
    comp_mes = mes - 1 if mes > 1 else 12
    comp_ano = ano if mes > 1 else ano - 1

    eventos: List[Dict] = []

    def add(d: date, desc: str, det: str, cat: str, clts: List, regime: List[str]):
        if d.month == mes and d.year == ano:
            eventos.append({
                "data": d.isoformat(),
                "descricao": desc,
                "detalhes": det,
                "categoria": cat,
                "cor": CORES[cat],
                "clientes": clts,
                "regime_alvo": regime,
            })

    # ── Agrupa clientes por regime / UF / município ─────────────────────
    simples  = [c for c in clientes if c.get("optante_simples")]
    nao_simples = [c for c in clientes if not c.get("optante_simples")]
    presumido = [c for c in nao_simples if c.get("regime_tributario") in ("presumido","lp","lucro_presumido","Lucro Presumido")]
    real      = [c for c in nao_simples if c.get("regime_tributario") in ("real","lr","lucro_real","Lucro Real")]
    todos     = clientes

    def cli_list(lst): return [{"id":c["id"],"nome":c["razao_social"]} for c in lst]

    # ── FEDERAL ─────────────────────────────────────────────────────────

    # DAS — Simples Nacional (dia 20)
    if simples:
        add(_dia(ano,mes,20,feriados),
            "DAS — Simples Nacional",
            f"Competência {comp_mes:02d}/{comp_ano}",
            "federal", cli_list(simples), ["simples"])

    # DARF IRPJ/CSLL — Lucro Presumido (trimestral — vence último dia útil de jan/abr/jul/out)
    if presumido and mes in (1, 4, 7, 10):
        add(_udu(ano,mes,feriados),
            "DARF IRPJ + CSLL — Lucro Presumido",
            f"Apuração trimestral competência {comp_mes:02d}/{comp_ano}. Códigos 2089 (IRPJ) e 2372 (CSLL).",
            "federal", cli_list(presumido), ["presumido"])

    # DARF IRPJ estimativa — Lucro Real (último dia útil mensal)
    if real:
        add(_udu(ano,mes,feriados),
            "DARF IRPJ Estimativa + CSLL — Lucro Real",
            f"Competência {comp_mes:02d}/{comp_ano}. Códigos 1599 (IRPJ) e 2484 (CSLL).",
            "federal", cli_list(real), ["real"])

    # PIS/COFINS — Lucro Presumido e Real (último dia útil)
    if presumido or real:
        alvo = presumido + real
        add(_udu(ano,mes,feriados),
            "DARF PIS/COFINS",
            f"Competência {comp_mes:02d}/{comp_ano}. Cumulativo: cód 8109/2172. Não-cumulativo: cód 6912/5856.",
            "federal", cli_list(alvo), ["presumido","real"])

    # DARF IRRF Folha (dia 20)
    if todos:
        add(_dia(ano,mes,20,feriados),
            "DARF IRRF — Rendimentos do Trabalho",
            f"Competência {comp_mes:02d}/{comp_ano}. Cód 0561 (empregados) / 0588 (autônomos).",
            "federal", cli_list(todos), ["todos"])

    # GPS / INSS Patronal (dia 20)
    if todos:
        add(_dia(ano,mes,20,feriados),
            "GPS — INSS Patronal + Empregado",
            f"Competência {comp_mes:02d}/{comp_ano}. Cód 2100 (empresa) + alíquota progressiva segurado.",
            "previdenciario", cli_list(todos), ["todos"])

    # FGTS Digital (dia 20)
    if todos:
        add(_dia(ano,mes,20,feriados),
            "DAE — FGTS Digital",
            f"Competência {comp_mes:02d}/{comp_ano}. 8% sobre remuneração bruta.",
            "previdenciario", cli_list(todos), ["todos"])

    # DCTFWeb (dia 15)
    if todos:
        add(_dia(ano,mes,15,feriados),
            "DCTFWeb — Declaração",
            f"Competência {comp_mes:02d}/{comp_ano}. Confessa débitos INSS + IRRF.",
            "acessoria", cli_list(todos), ["todos"])

    # eSocial S-1299 fechamento (dia 15)
    if todos:
        add(_dia(ano,mes,15,feriados),
            "eSocial S-1299 — Fechamento de Folha",
            f"Competência {comp_mes:02d}/{comp_ano}.",
            "acessoria", cli_list(todos), ["todos"])

    # EFD-Contribuições (dia 14 do 2º mês seguinte à competência)
    # competência de -2 meses
    comp2_mes = mes - 2 if mes > 2 else (12 + mes - 2)
    comp2_ano = ano if mes > 2 else ano - 1
    if nao_simples:
        add(_dia(ano,mes,14,feriados),
            "EFD-Contribuições — Transmissão",
            f"Competência {comp2_mes:02d}/{comp2_ano}. Lucro Presumido e Real.",
            "acessoria", cli_list(nao_simples), ["presumido","real"])

    # SPED Fiscal / EFD-ICMS-IPI (dia 25)
    if nao_simples:
        add(_dia(ano,mes,25,feriados),
            "SPED Fiscal (EFD-ICMS/IPI) — Transmissão",
            f"Competência {comp_mes:02d}/{comp_ano}.",
            "acessoria", cli_list(nao_simples), ["presumido","real"])

    # EFD-Reinf R-2099 / R-4099 — fechamento (dia 15)
    if todos:
        add(_dia(ano,mes,15,feriados),
            "EFD-Reinf — Fechamento Periódico",
            f"Competência {comp_mes:02d}/{comp_ano}. R-2099 + R-4099.",
            "acessoria", cli_list(todos), ["todos"])

    # ── Anuais (mostrados no mês correto) ──────────────────────────────

    # DEFIS — Simples Nacional (último dia de março)
    if simples and mes == 3:
        add(_udu(ano,mes,feriados),
            "DEFIS — Declaração Anual Simples Nacional",
            f"Ano-calendário {ano-1}. Prazo: último dia útil de março.",
            "acessoria", cli_list(simples), ["simples"])

    # ECD — Lucro Real/Presumido acima R$4,8M (último dia útil de junho)
    if nao_simples and mes == 6:
        add(_udu(ano,mes,feriados),
            "ECD — Escrituração Contábil Digital",
            f"Exercício {ano-1}. Obrigatório para Lucro Real e Presumido acima de R$ 4,8 mi.",
            "acessoria", cli_list(nao_simples), ["presumido","real"])

    # ECF — todos exceto Simples (último dia útil de julho)
    if nao_simples and mes == 7:
        add(_udu(ano,mes,feriados),
            "ECF — Escrituração Contábil Fiscal",
            f"Exercício {ano-1}.",
            "acessoria", cli_list(nao_simples), ["presumido","real"])

    # IRPF — sócios PF (último dia útil de maio)
    if todos and mes == 5:
        add(_udu(ano,mes,feriados),
            "IRPF — Declaração de Ajuste Anual (sócios PF)",
            f"Exercício {ano-1}. Lembre de comunicar seus clientes.",
            "acessoria", [], ["todos"])

    # ── ESTADUAL — ICMS ────────────────────────────────────────────────

    # Agrupa clientes não-Simples por UF
    ufs_clientes: Dict[str, List] = {}
    for c in nao_simples:
        uf = (c.get("uf") or "").upper()
        if uf and uf in ICMS_DIA_POR_UF:
            ufs_clientes.setdefault(uf, []).append(c)

    for uf, clts in ufs_clientes.items():
        dia_icms = ICMS_DIA_POR_UF[uf]
        add(_dia(ano,mes,dia_icms,feriados),
            f"ICMS — GIA/SPED Fiscal ({uf})",
            f"Competência {comp_mes:02d}/{comp_ano}. Vencimento dia {dia_icms} (SEFAZ {uf}).",
            "estadual", cli_list(clts), ["presumido","real"])

    # Simples paga ICMS dentro do DAS — apenas aviso anual de GIA-SN (março)
    # Não geramos evento separado.

    # ── MUNICIPAL — ISS ────────────────────────────────────────────────

    # Agrupa todos os clientes por município (IBGE)
    ibge_clientes: Dict[str, List] = {}
    for c in todos:
        ibge = c.get("codigo_ibge") or ""
        if ibge:
            ibge_clientes.setdefault(ibge, []).append(c)
        else:
            # Sem IBGE: usa município como chave
            mun = (c.get("municipio") or "").lower().strip()
            if mun:
                ibge_clientes.setdefault(f"mun:{mun}", []).append(c)

    for ibge, clts in ibge_clientes.items():
        dia_iss = iss_dia(ibge if not ibge.startswith("mun:") else None)
        mun_nome = clts[0].get("municipio") or ibge
        add(_dia(ano,mes,dia_iss,feriados),
            f"ISS — DMS / Guia ({mun_nome})",
            f"Competência {comp_mes:02d}/{comp_ano}. ISS vence dia {dia_iss} no município.",
            "municipal", cli_list(clts), ["todos"])

    # ── CERTIFICADOS DIGITAIS ──────────────────────────────────────────────
    for c in clientes:
        nome = c.get("nome_fantasia") or c["razao_social"]
        cli = [{"id": c["id"], "nome": c["razao_social"]}]
        for tipo, path_key, venc_key in [
            ("NFS-e", "nfse_certificado_path", "nfse_certificado_vencimento"),
            ("NF-e",  "nfe_certificado_path",  "nfe_certificado_vencimento"),
        ]:
            if not c.get(path_key):
                continue
            venc_str = c.get(venc_key)
            if not venc_str:
                continue
            try:
                venc = date.fromisoformat(str(venc_str)[:10])
            except ValueError:
                continue
            # Evento no dia do vencimento
            if venc.year == ano and venc.month == mes:
                eventos.append({
                    "data": venc.isoformat(),
                    "descricao": f"Certificado A1 {tipo} vence — {nome}",
                    "detalhes": f"O certificado digital A1 ({tipo}) de {nome} vence hoje. Providencie a renovação imediatamente.",
                    "categoria": "certificado",
                    "cor": "#dc2626",
                    "clientes": cli,
                    "regime_alvo": ["todos"],
                })
            # Aviso 30 dias antes
            aviso = venc - timedelta(30)
            if aviso.year == ano and aviso.month == mes:
                eventos.append({
                    "data": aviso.isoformat(),
                    "descricao": f"Certificado A1 {tipo} vence em 30 dias — {nome}",
                    "detalhes": f"O certificado digital A1 ({tipo}) de {nome} vence em {venc.strftime('%d/%m/%Y')}. Solicite a renovação.",
                    "categoria": "certificado",
                    "cor": "#f59e0b",
                    "clientes": cli,
                    "regime_alvo": ["todos"],
                })

    # Ordena por data
    eventos.sort(key=lambda e: e["data"])
    return eventos


def proximos_vencimentos(
    clientes: List[Dict],
    dias: int = 30,
) -> List[Dict[str, Any]]:
    """Retorna eventos dos próximos `dias` dias a partir de hoje."""
    hoje = date.today()
    resultado = []
    meses_vistos = set()
    d = hoje
    while d <= hoje + timedelta(dias):
        chave = (d.year, d.month)
        if chave not in meses_vistos:
            resultado.extend(gerar_eventos(d.year, d.month, clientes))
            meses_vistos.add(chave)
        d += timedelta(30)

    # Filtra para o intervalo
    fim = (hoje + timedelta(dias)).isoformat()
    hoje_str = hoje.isoformat()
    resultado = [e for e in resultado if hoje_str <= e["data"] <= fim]
    resultado.sort(key=lambda e: e["data"])
    return resultado
