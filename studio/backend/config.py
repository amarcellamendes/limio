from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "ML Studio"
    DEBUG: bool = False
    SECRET_KEY: str = "mude-esta-chave-em-producao-studio-2026"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 30  # 30 dias — app no celular não fica pedindo login

    DATABASE_URL: str = "sqlite+aiosqlite:///./data/studio.db"

    # Diretório persistente (vídeos brutos, editados, logos, músicas).
    # No Railway: monte um Volume em /data e configure DATA_DIR=/data
    DATA_DIR: str = "./data"

    # Cadastro aberto? Se False, só o primeiro usuário consegue se cadastrar
    # (os demais são convidados pelo dono na tela de Equipe).
    REGISTRO_ABERTO: bool = False

    # Claude — a "diretora de edição" que decide cortes, gancho, destaques e CTA
    # Configure em Railway → Variables: ANTHROPIC_API_KEY=sk-ant-...
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODELO: str = "claude-opus-5-5"
    CLAUDE_ESFORCO: str = "high"  # low | medium | high | xhigh | max

    # Transcrição com marcação de tempo por palavra (API compatível com Whisper).
    # OpenAI: https://api.openai.com/v1  (modelo whisper-1)
    # Groq:   https://api.groq.com/openai/v1  (modelo whisper-large-v3, mais barato)
    TRANSCRICAO_URL: str = "https://api.openai.com/v1"
    TRANSCRICAO_API_KEY: str = ""
    TRANSCRICAO_MODELO: str = "whisper-1"

    # Pasta com as fontes (.ttf) usadas nas legendas
    FONTS_DIR: str = "./fonts"

    # Quantos vídeos renderizam ao mesmo tempo (render é pesado em CPU)
    RENDERS_SIMULTANEOS: int = 1

    class Config:
        env_file = ".env"


settings = Settings()
