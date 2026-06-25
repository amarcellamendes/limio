from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    APP_NAME: str = "Limio"
    DEBUG: bool = True
    SECRET_KEY: str = "mude-esta-chave-em-producao-super-secreta-2026"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    DATABASE_URL: str = "sqlite+aiosqlite:///./emissor_notas.db"

    # NFS-e Nacional (SEFAZ)
    NFSE_NACIONAL_URL_PRODUCAO: str = "https://nfse.sefaz.gov.br/api"
    NFSE_NACIONAL_URL_HOMOLOGACAO: str = "https://homologacao.nfse.sefaz.gov.br/api"
    NFSE_AMBIENTE: str = "2"  # 1=producao, 2=homologacao

    # NFe.io (Agregadora comercial — fallback)
    NFEIO_API_URL: str = "https://api.nfe.io/v1"
    NFEIO_API_KEY: Optional[str] = None

    # NF-e SEFAZ (produtos — modelo 55)
    NFE_AMBIENTE: str = "2"  # 1=producao, 2=homologacao

    # Diretório persistente de dados (certificados, banco etc.)
    DATA_DIR: str = "./data"

    # Modo mock (True = não chama APIs reais, gera dados simulados)
    MOCK_MODE: bool = True

    class Config:
        env_file = ".env"


settings = Settings()
