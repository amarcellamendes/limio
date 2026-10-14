"""A "diretora de edição": Claude lê a transcrição e decide cortes, gancho, destaques e CTA.

Ela não mexe em pixels — devolve um plano em JSON (formato fixo por schema) que o
render.py executa com ffmpeg. Assim a edição é previsível e dá pra revisar antes.
"""
import json

import anthropic

from ..config import settings
from . import edicao


class ErroDiretor(RuntimeError):
    pass


def disponivel() -> bool:
    return bool(settings.ANTHROPIC_API_KEY)


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR, _INT = {"type": "string"}, {"type": "integer"}

SCHEMA_PLANO = _obj({
    "titulo": _STR,
    "resumo": _STR,
    "manter": {"type": "array", "items": _obj({"de": _INT, "ate": _INT, "motivo": _STR})},
    "gancho": _obj({"texto": _STR, "destaque": _STR}),
    "impactos": {"type": "array", "items": _obj({"palavra": _INT, "texto": _STR})},
    "zooms": {"type": "array", "items": _INT},
    "titulos": {"type": "array", "items": _obj({"palavra": _INT, "texto": _STR})},
    "cta": _obj({"texto": _STR}),
    "legenda_post": _STR,
    "hashtags": {"type": "array", "items": _STR},
})

SISTEMA = """Você é a editora de vídeos curtos (Reels, TikTok, Shorts) de uma criadora de conteúdo brasileira. \
Você recebe a transcrição do vídeo BRUTO, palavra por palavra, cada palavra com um número (índice) e o tempo em segundos. \
Seu trabalho é decidir a edição; um programa executa os cortes e a arte a partir do seu plano.

Como decidir os cortes (campo "manter"):
- "manter" lista faixas de índices de palavras (de/ate, inclusive) que ficam no vídeo, em ordem. Todo o resto é cortado.
- Quando a pessoa repete uma frase (errou e gravou de novo), fique com a ÚLTIMA tomada completa, salvo pedido diferente.
- Corte gaguejos, palavras soltas de hesitação ("é...", "ahn", "né" vazio), frases abandonadas no meio, falas para a equipe \
("corta", "de novo", "peraí", "vou gravar de novo") e silêncio no começo e no fim.
- Nunca corte no meio de uma ideia que muda o sentido. Mantenha a ordem original das falas.
- Pausas longas DENTRO de uma faixa são cortadas automaticamente; você não precisa quebrar faixas por causa delas.

Arte e ritmo:
- "gancho": frase curta (até 8 palavras) que aparece em letras grandes nos primeiros segundos. Use as palavras da própria fala \
quando forem fortes; senão, escreva um gancho fiel ao conteúdo. "destaque" é a palavra do gancho que vai na cor de destaque.
- "impactos": 3 a 8 palavras-chave (números, valores, termos fortes do nicho) que aparecem grandes na tela quando são ditas. \
"palavra" é o índice da palavra no momento certo; "texto" é o que aparece (pode ser "R$ 5 MIL", "FATOR R").
- "zooms": índices de palavras onde dar um zoom de ênfase (momentos de virada, revelação, números). Use com moderação.
- "titulos": títulos de seção quando o vídeo tem passos ou tópicos ("PASSO 1", "ERRO Nº 2"). Lista vazia se não tiver.
- "cta": chamada para ação no final, alinhada ao CTA padrão da marca, ao nicho e ao assunto.
- "legenda_post": legenda pronta para o post (2 a 5 linhas, tom da marca, termine com a CTA). "hashtags": 5 a 10, sem #.
- "titulo": nome curto do vídeo. "resumo": em 1 a 3 frases, o que você fez na edição, falando com a criadora ("Cortei...").

Todos os índices precisam existir na transcrição e estar dentro das faixas mantidas. Escreva tudo em português do Brasil. \
Siga a identidade e as instruções fixas da criadora; o pedido específico do vídeo tem prioridade sobre as instruções fixas."""


def _transcricao_formatada(palavras: list[dict]) -> str:
    linhas = []
    for f in edicao.frases(palavras):
        corpo = " ".join(f"{i}:{palavras[i]['w']}" for i in range(f["de"], f["ate"] + 1))
        linhas.append(f"[{f['ini']:.1f}s–{f['fim']:.1f}s] {corpo}")
    return "\n".join(linhas)


def _identidade_formatada(ident) -> str:
    return (
        f"Marca: {ident.nome_marca} {ident.arroba}\n"
        f"Nicho: {ident.nicho}\n"
        f"Público: {ident.publico}\n"
        f"Tom de voz: {ident.tom_de_voz}\n"
        f"CTA padrão: {ident.cta_padrao}\n"
        f"Instruções fixas da criadora: {ident.instrucoes_fixas}"
    )


async def planejar(palavras: list[dict], identidade, pedido: str, duracao: float,
                   plano_anterior: dict | None = None, ajuste: str = "") -> dict:
    """Pede o plano de edição ao Claude. Com `plano_anterior` + `ajuste`, revisa a edição."""
    if not disponivel():
        raise ErroDiretor("ANTHROPIC_API_KEY não configurada")

    conteudo = (
        f"<identidade>\n{_identidade_formatada(identidade)}\n</identidade>\n\n"
        f"<pedido_do_video>\n{pedido or '(sem pedido específico — siga a identidade e as instruções fixas)'}\n</pedido_do_video>\n\n"
        f"Duração do bruto: {duracao:.1f}s. Total de palavras: {len(palavras)} (índices 0 a {len(palavras) - 1}).\n\n"
        f"<transcricao>\n{_transcricao_formatada(palavras)}\n</transcricao>"
    )
    if plano_anterior:
        anterior = {k: plano_anterior.get(k) for k in SCHEMA_PLANO["properties"]}
        conteudo += (
            f"\n\n<edicao_atual>\n{json.dumps(anterior, ensure_ascii=False)}\n</edicao_atual>\n\n"
            f"<ajuste_pedido>\n{ajuste}\n</ajuste_pedido>\n\n"
            "Refaça o plano aplicando o ajuste pedido e mantendo o resto da edição atual."
        )

    cliente = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        async with cliente.beta.messages.stream(
            model=settings.CLAUDE_MODELO,
            max_tokens=64000,
            system=SISTEMA,
            thinking={"type": "adaptive"},
            output_config={
                "effort": settings.CLAUDE_ESFORCO,
                "format": {"type": "json_schema", "schema": SCHEMA_PLANO},
            },
            # Se o modelo principal recusar por engano, a API tenta outro modelo na mesma chamada
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": conteudo}],
        ) as stream:
            resposta = await stream.get_final_message()
    except anthropic.AuthenticationError:
        raise ErroDiretor("Chave da Anthropic inválida")
    except anthropic.RateLimitError:
        raise ErroDiretor("Limite de uso da IA atingido — tente de novo em alguns minutos")
    except anthropic.APIStatusError as e:
        raise ErroDiretor(f"Erro da IA ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise ErroDiretor("Sem conexão com a IA")

    if resposta.stop_reason == "refusal":
        raise ErroDiretor("A IA recusou este vídeo")
    if resposta.stop_reason == "max_tokens":
        raise ErroDiretor("O vídeo é longo demais para um único plano — divida em partes")

    texto = "".join(b.text for b in resposta.content if b.type == "text")
    try:
        plano = json.loads(texto)
    except json.JSONDecodeError:
        raise ErroDiretor("A IA devolveu um plano inválido")
    return validar(plano, len(palavras))


def validar(plano: dict, total: int) -> dict:
    """Descarta índices fora da transcrição para nunca quebrar o render."""
    ok = lambda i: isinstance(i, int) and 0 <= i < total  # noqa: E731
    plano["manter"] = [
        f for f in plano.get("manter", [])
        if ok(f.get("de")) and ok(f.get("ate")) and f["de"] <= f["ate"]
    ]
    mantidos = set(edicao.indices_mantidos(plano, total))
    plano["impactos"] = [x for x in plano.get("impactos", []) if x.get("palavra") in mantidos]
    plano["titulos"] = [x for x in plano.get("titulos", []) if x.get("palavra") in mantidos]
    plano["zooms"] = [i for i in plano.get("zooms", []) if i in mantidos]
    plano["hashtags"] = [h.lstrip("#").strip() for h in plano.get("hashtags", []) if h.strip()]
    plano["manter_tempo"] = []
    plano["origem"] = "claude"
    return plano
