from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from .config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        from . import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_sqlite)


def _migrate_sqlite(conn):
    """Adiciona colunas novas sem perder dados (compatível com SQLite)."""
    from sqlalchemy import text, inspect
    insp = inspect(conn)

    def add_col(table, col, typedef):
        if table not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns(table)]
        if col not in cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}"))

    # Clientes — novos campos
    add_col("clientes", "emite_nfse",              "BOOLEAN DEFAULT 1")
    add_col("clientes", "emite_nfe",               "BOOLEAN DEFAULT 0")
    add_col("clientes", "nfe_provider",            "VARCHAR(20) DEFAULT 'mock'")
    add_col("clientes", "nfe_api_key",             "VARCHAR(200)")
    add_col("clientes", "nfe_company_id",          "VARCHAR(100)")
    add_col("clientes", "nfe_serie",               "VARCHAR(5) DEFAULT '1'")
    add_col("clientes", "nfe_certificado_path",    "VARCHAR(300)")
    add_col("clientes", "nfe_certificado_senha",   "VARCHAR(200)")
    add_col("clientes", "nfse_serie_rps",          "VARCHAR(20) DEFAULT 'RPS'")
    add_col("clientes", "logradouro",              "VARCHAR(200)")
    add_col("clientes", "numero",                  "VARCHAR(20)")
    add_col("clientes", "bairro",                  "VARCHAR(100)")
    add_col("clientes", "boleto_ativo",            "BOOLEAN DEFAULT 0")
    add_col("clientes", "boleto_provider",         "VARCHAR(20)")
    add_col("clientes", "boleto_api_key",          "VARCHAR(200)")
    add_col("clientes", "boleto_dias_vencimento",  "INTEGER DEFAULT 3")

    # Notas — novos campos
    add_col("notas", "nota_substituida_id",   "INTEGER")
    add_col("notas", "boleto_url",            "VARCHAR(500)")
    add_col("notas", "boleto_linha_digitavel","VARCHAR(120)")
    add_col("notas", "boleto_codigo_barras",  "VARCHAR(60)")
    add_col("notas", "boleto_vencimento",     "DATETIME")
    add_col("notas", "boleto_status",         "VARCHAR(20)")

    # Contratos — colunas extras (tabela criada pelo create_all, mas pode ser banco antigo)
    add_col("contratos", "gerar_boleto",  "BOOLEAN DEFAULT 0")
    add_col("contratos", "total_emitido", "INTEGER DEFAULT 0")
    add_col("contratos", "atualizado_em", "DATETIME")
