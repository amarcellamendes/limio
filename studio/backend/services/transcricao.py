"""Transcrição com tempo por palavra (API compatível com Whisper: OpenAI ou Groq)."""
import httpx

from ..config import settings


class ErroTranscricao(RuntimeError):
    pass


def disponivel() -> bool:
    return bool(settings.TRANSCRICAO_API_KEY)


async def transcrever(caminho_audio: str, dicas: str = "") -> list[dict]:
    """Devolve [{"w": "palavra", "ini": 1.23, "fim": 1.50}, ...] na ordem falada.

    `dicas` vai como prompt do Whisper — ajuda a acertar termos do nicho
    (ex.: "Fator R, pró-labore, CNPJ, Simples Nacional").
    """
    if not disponivel():
        return []

    url = settings.TRANSCRICAO_URL.rstrip("/") + "/audio/transcriptions"
    dados = {
        "model": settings.TRANSCRICAO_MODELO,
        "language": "pt",
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
    }
    if dicas:
        dados["prompt"] = dicas[:800]

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0)) as cli:
        with open(caminho_audio, "rb") as f:
            r = await cli.post(
                url,
                headers={"Authorization": f"Bearer {settings.TRANSCRICAO_API_KEY}"},
                data=dados,
                files={"file": ("audio.mp3", f, "audio/mpeg")},
            )
    if r.status_code >= 400:
        raise ErroTranscricao(f"Transcrição falhou ({r.status_code}): {r.text[:400]}")

    corpo = r.json()
    palavras = []
    for p in corpo.get("words") or []:
        texto = (p.get("word") or "").strip()
        if texto:
            palavras.append({"w": texto, "ini": round(float(p["start"]), 3), "fim": round(float(p["end"]), 3)})
    return palavras
