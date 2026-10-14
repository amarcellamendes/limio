"""Regras determinísticas da edição: frases, trechos mantidos, linha do tempo de saída.

A IA decide *o quê* fica (quais palavras, gancho, destaques). Este módulo transforma
essa decisão em tempos exatos de corte — corte seco nas pausas, sem comer sílabas.
"""
import re
import unicodedata
from dataclasses import dataclass

FOLGA_ANTES = 0.06   # segundos antes da 1ª palavra de um trecho
FOLGA_DEPOIS = 0.14  # depois da última (o fim da palavra no Whisper costuma vir cedo)
TRECHO_MINIMO = 0.20

PREENCHIMENTOS = {"é", "eh", "ahn", "hã", "ã", "hum", "hmm", "uh", "ah", "tipo", "né"}
TERMOS_IMPACTO = {
    "imposto", "impostos", "cnpj", "mei", "simples", "fator", "pró-labore", "prolabore",
    "economia", "economizar", "lucro", "multa", "receita", "leão", "clt", "nunca", "erro",
    "grátis", "dinheiro", "faturamento", "faturar", "irpf", "dívida", "segredo",
}


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"[^\w%$]+", "", texto)


@dataclass
class Trecho:
    ini: float   # no vídeo bruto
    fim: float
    saida: float = 0.0  # onde começa no vídeo editado

    @property
    def duracao(self) -> float:
        return self.fim - self.ini


# ───────────────────────────── frases ─────────────────────────────

def frases(palavras: list[dict], pausa: float = 0.6) -> list[dict]:
    """Agrupa palavras em frases quebrando nas pausas longas. [{id, de, ate, ini, fim, texto}]"""
    grupos, atual = [], []
    for i, p in enumerate(palavras):
        if atual and p["ini"] - palavras[atual[-1]]["fim"] > pausa:
            grupos.append(atual)
            atual = []
        atual.append(i)
    if atual:
        grupos.append(atual)
    return [
        {
            "id": n, "de": g[0], "ate": g[-1],
            "ini": palavras[g[0]]["ini"], "fim": palavras[g[-1]]["fim"],
            "texto": " ".join(palavras[i]["w"] for i in g),
        }
        for n, g in enumerate(grupos)
    ]


# ─────────────────────── plano automático (sem IA) ───────────────────────

def _inicio(frase: dict, palavras: list[dict], n: int = 3) -> list[str]:
    return [normalizar(palavras[i]["w"]) for i in range(frase["de"], min(frase["ate"], frase["de"] + n - 1) + 1)]


def plano_automatico(palavras: list[dict], identidade, duracao: float, silencios: list[tuple[float, float]]) -> dict:
    """Plano sem IA: corta pausas e tomadas repetidas (fica a última), gancho = 1ª frase."""
    if not palavras:
        manter_tempo = trechos_por_silencio(silencios, duracao, identidade.pausa_maxima)
        return {
            "origem": "automatico", "titulo": "", "resumo": "Cortei as pausas pelo áudio (sem transcrição configurada, então sem legenda).",
            "manter": [], "manter_tempo": [[t.ini, t.fim] for t in manter_tempo],
            "gancho": {"texto": "", "destaque": ""}, "impactos": [], "zooms": [], "titulos": [],
            "cta": {"texto": identidade.cta_padrao}, "legenda_post": "", "hashtags": [],
        }

    fs = frases(palavras)
    descartar = set()
    for i, f in enumerate(fs):
        inicio = _inicio(f, palavras)
        if len(inicio) < 2:
            continue
        for g in fs[i + 1:i + 4]:
            if _inicio(g, palavras)[:len(inicio)] == inicio:
                descartar.add(f["id"])  # repetiu a frase depois → fica a última tomada
                break
    for f in fs:
        if all(normalizar(palavras[i]["w"]) in PREENCHIMENTOS for i in range(f["de"], f["ate"] + 1)):
            descartar.add(f["id"])

    mantidas = [f for f in fs if f["id"] not in descartar]
    manter = [{"de": f["de"], "ate": f["ate"], "motivo": "fala"} for f in mantidas]

    gancho = ""
    if mantidas:
        ws = [palavras[i]["w"] for i in range(mantidas[0]["de"], mantidas[0]["ate"] + 1)]
        gancho = " ".join(ws[:8])

    impactos = []
    for f in mantidas:
        for i in range(f["de"], f["ate"] + 1):
            n = normalizar(palavras[i]["w"])
            if n in TERMOS_IMPACTO or any(c.isdigit() for c in n):
                impactos.append({"palavra": i, "texto": palavras[i]["w"]})
    impactos = impactos[:: max(1, len(impactos) // 6 or 1)][:6]

    return {
        "origem": "automatico",
        "titulo": gancho[:60],
        "resumo": f"Cortei as pausas e {len(descartar)} tomada(s) repetida(s) ou vazia(s), ficando sempre com a última.",
        "manter": manter, "manter_tempo": [],
        "gancho": {"texto": gancho, "destaque": ""},
        "impactos": impactos, "zooms": [x["palavra"] for x in impactos[::2]], "titulos": [],
        "cta": {"texto": identidade.cta_padrao},
        "legenda_post": "", "hashtags": [],
    }


# ─────────────────────── plano → tempos de corte ───────────────────────

def indices_mantidos(plano: dict, total: int) -> list[int]:
    sel = set()
    for faixa in plano.get("manter", []):
        de, ate = int(faixa.get("de", 0)), int(faixa.get("ate", -1))
        sel.update(range(max(0, de), min(total - 1, ate) + 1))
    return sorted(sel)


def trechos_por_palavras(palavras: list[dict], indices: list[int], duracao: float, pausa_maxima: float) -> list[Trecho]:
    """Converte as palavras mantidas em trechos de tempo, cortando pausas > pausa_maxima."""
    if not indices:
        return []
    grupos, atual = [], [indices[0]]
    for a, b in zip(indices, indices[1:]):
        consecutivo = b == a + 1
        if consecutivo and palavras[b]["ini"] - palavras[a]["fim"] <= pausa_maxima:
            atual.append(b)
        else:
            grupos.append(atual)
            atual = [b]
    grupos.append(atual)

    trechos = []
    for g in grupos:
        primeira, ultima = g[0], g[-1]
        limite_ini = palavras[primeira - 1]["fim"] if primeira > 0 else 0.0
        limite_fim = palavras[ultima + 1]["ini"] if ultima + 1 < len(palavras) else duracao
        ini = max(limite_ini, palavras[primeira]["ini"] - FOLGA_ANTES, 0.0)
        fim = min(limite_fim, palavras[ultima]["fim"] + FOLGA_DEPOIS, duracao or 1e9)
        if fim - ini >= TRECHO_MINIMO:
            trechos.append(Trecho(ini, fim))
    return _unir(trechos)


def trechos_por_silencio(silencios: list[tuple[float, float]], duracao: float, pausa_maxima: float) -> list[Trecho]:
    """Sem transcrição: mantém tudo que não é silêncio longo (deixa um respiro nas bordas)."""
    trechos, cursor = [], 0.0
    respiro = min(0.12, pausa_maxima / 2)
    for ini, fim in silencios:
        if fim - ini <= pausa_maxima:
            continue
        if ini + respiro - cursor >= TRECHO_MINIMO:
            trechos.append(Trecho(cursor, ini + respiro))
        cursor = max(cursor, fim - respiro)
    if duracao - cursor >= TRECHO_MINIMO:
        trechos.append(Trecho(cursor, duracao))
    return _unir(trechos)


def _unir(trechos: list[Trecho]) -> list[Trecho]:
    trechos = sorted(trechos, key=lambda t: t.ini)
    unidos: list[Trecho] = []
    for t in trechos:
        if unidos and t.ini <= unidos[-1].fim + 0.02:
            unidos[-1].fim = max(unidos[-1].fim, t.fim)
        else:
            unidos.append(Trecho(t.ini, t.fim))
    saida = 0.0
    for t in unidos:
        t.saida = saida
        saida += t.duracao
    return unidos


def trechos_do_plano(plano: dict, palavras: list[dict], duracao: float, pausa_maxima: float) -> list[Trecho]:
    if plano.get("manter_tempo"):
        return _unir([Trecho(float(a), float(b)) for a, b in plano["manter_tempo"]])
    return trechos_por_palavras(palavras, indices_mantidos(plano, len(palavras)), duracao, pausa_maxima)


def para_saida(t: float, trechos: list[Trecho]) -> float | None:
    """Tempo do bruto → tempo no editado (None se o instante foi cortado)."""
    for tr in trechos:
        if tr.ini <= t <= tr.fim:
            return tr.saida + (t - tr.ini)
    return None


def palavras_na_saida(palavras: list[dict], indices: list[int], trechos: list[Trecho]) -> list[dict]:
    """Palavras mantidas com tempos já na linha do tempo do vídeo editado."""
    resultado = []
    for i in indices:
        p = palavras[i]
        ini = para_saida(p["ini"], trechos)
        fim = para_saida(min(p["fim"], _fim_do_trecho(p["ini"], trechos)), trechos)
        if ini is None or fim is None:
            continue
        resultado.append({"i": i, "w": p["w"], "ini": ini, "fim": max(fim, ini + 0.05)})
    return resultado


def _fim_do_trecho(t: float, trechos: list[Trecho]) -> float:
    for tr in trechos:
        if tr.ini <= t <= tr.fim:
            return tr.fim
    return t
