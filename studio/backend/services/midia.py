"""Operações de mídia com ffmpeg/ffprobe: metadados, áudio, silêncios e miniatura."""
import asyncio
import json
import re
from dataclasses import dataclass


class ErroMidia(RuntimeError):
    pass


async def executar(*args: str, timeout: float | None = None) -> str:
    """Roda um comando e devolve stderr+stdout. Levanta ErroMidia se falhar."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ErroMidia(f"{args[0]} demorou demais e foi interrompido")
    texto = (out or b"").decode("utf-8", "replace") + (err or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        raise ErroMidia(f"{args[0]} falhou: {texto[-1500:]}")
    return texto


@dataclass
class InfoVideo:
    duracao: float
    largura: int
    altura: int
    fps: float
    tem_audio: bool


async def analisar(caminho: str) -> InfoVideo:
    saida = await executar(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", caminho,
    )
    dados = json.loads(saida[saida.index("{"):])
    video = next((s for s in dados.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ErroMidia("O arquivo enviado não tem trilha de vídeo")
    audio = any(s.get("codec_type") == "audio" for s in dados.get("streams", []))

    largura, altura = int(video.get("width", 0)), int(video.get("height", 0))
    # Vídeo de celular costuma vir "deitado" com metadado de rotação
    rotacao = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotacao = int(sd["rotation"])
    rotacao = int(video.get("tags", {}).get("rotate", rotacao) or 0)
    if abs(rotacao) % 180 == 90:
        largura, altura = altura, largura

    num, _, den = (video.get("avg_frame_rate") or "30/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    duracao = float(dados.get("format", {}).get("duration") or video.get("duration") or 0)
    return InfoVideo(duracao, largura, altura, fps if fps > 0 else 30.0, audio)


async def extrair_audio(caminho: str, destino_mp3: str) -> None:
    """Áudio mono 16 kHz em MP3 — leve o bastante para mandar à transcrição (≈ 70 min em 25 MB)."""
    await executar(
        "ffmpeg", "-y", "-v", "error", "-i", caminho, "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "48k", destino_mp3,
    )


_RE_SIL_INI = re.compile(r"silence_start: (-?[\d.]+)")
_RE_SIL_FIM = re.compile(r"silence_end: ([\d.]+)")


async def detectar_silencios(caminho: str, minimo: float = 0.35, limiar_db: int = -35) -> list[tuple[float, float]]:
    saida = await executar(
        "ffmpeg", "-hide_banner", "-i", caminho, "-vn",
        "-af", f"silencedetect=noise={limiar_db}dB:d={minimo}", "-f", "null", "-",
    )
    silencios, inicio = [], None
    for linha in saida.splitlines():
        if (m := _RE_SIL_INI.search(linha)):
            inicio = max(0.0, float(m.group(1)))
        elif (m := _RE_SIL_FIM.search(linha)) and inicio is not None:
            silencios.append((inicio, float(m.group(1))))
            inicio = None
    return silencios


async def gerar_miniatura(caminho: str, destino_jpg: str, segundo: float = 1.0) -> None:
    await executar(
        "ffmpeg", "-y", "-v", "error", "-ss", f"{segundo:.2f}", "-i", caminho,
        "-frames:v", "1", "-vf", "scale=360:-2", destino_jpg,
    )
