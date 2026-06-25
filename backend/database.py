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
    import os
    from .config import settings
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(settings.DATA_DIR, "certs"), exist_ok=True)
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

    # Escritório — novos campos
    add_col("escritorios", "nfeio_api_key",        "VARCHAR(200)")
    add_col("escritorios", "nota_enviar_email",     "BOOLEAN DEFAULT 0")
    add_col("escritorios", "nota_pasta_destino",    "VARCHAR(500)")
    add_col("escritorios", "smtp_host",             "VARCHAR(200)")
    add_col("escritorios", "smtp_port",             "INTEGER DEFAULT 587")
    add_col("escritorios", "smtp_usuario",          "VARCHAR(200)")
    add_col("escritorios", "smtp_senha",            "VARCHAR(200)")

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
    add_col("clientes", "nfse_certificado_path",        "VARCHAR(500)")
    add_col("clientes", "nfse_certificado_senha",       "VARCHAR(200)")
    add_col("clientes", "nfse_certificado_vencimento",  "DATETIME")
    add_col("clientes", "nfe_certificado_vencimento",   "DATETIME")
    add_col("clientes", "limite_simples",               "FLOAT DEFAULT 4800000.0")
    add_col("clientes", "anexo_simples",                "VARCHAR(10)")
    add_col("clientes", "atividade_permite_fator_r",    "BOOLEAN DEFAULT 0")

    # Clientes — CNAE e responsável
    add_col("clientes", "cnae",            "VARCHAR(10)")
    add_col("clientes", "cnae_descricao",  "VARCHAR(300)")
    add_col("clientes", "responsavel_id",  "INTEGER REFERENCES usuarios(id)")

    # Contratos — dados de obra
    add_col("contratos", "is_construtora",      "BOOLEAN DEFAULT 0")
    add_col("contratos", "obra_cei",            "VARCHAR(30)")
    add_col("contratos", "obra_art",            "VARCHAR(50)")
    add_col("contratos", "obra_alvara",         "VARCHAR(50)")
    add_col("contratos", "obra_matricula",      "VARCHAR(50)")
    add_col("contratos", "obra_endereco",       "VARCHAR(300)")
    add_col("contratos", "obra_inss_aliquota",  "FLOAT DEFAULT 0")

    # Fornecedores
    if "fornecedores" not in insp.get_table_names():
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS fornecedores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                escritorio_id INTEGER NOT NULL REFERENCES escritorios(id),
                cnpj VARCHAR(18) NOT NULL,
                razao_social VARCHAR(300) NOT NULL,
                nome_fantasia VARCHAR(300),
                uf VARCHAR(2),
                municipio VARCHAR(100),
                email VARCHAR(200),
                telefone VARCHAR(30),
                categoria VARCHAR(100),
                ativo BOOLEAN DEFAULT 1,
                total_notas INTEGER DEFAULT 0,
                valor_total_notas FLOAT DEFAULT 0.0,
                ultima_nota_em DATETIME,
                criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))

    # ICMS Mensal
    if "icms_mensal" not in insp.get_table_names():
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS icms_mensal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                escritorio_id INTEGER NOT NULL REFERENCES escritorios(id),
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                competencia VARCHAR(7) NOT NULL,
                credito FLOAT DEFAULT 0.0,
                debito FLOAT DEFAULT 0.0,
                saldo FLOAT DEFAULT 0.0,
                origem VARCHAR(20) DEFAULT 'manual',
                observacao VARCHAR(300),
                criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))

    # Certidões
    if "certidoes" not in insp.get_table_names():
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS certidoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                escritorio_id INTEGER NOT NULL REFERENCES escritorios(id),
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                tipo VARCHAR(30) NOT NULL,
                nome_descritivo VARCHAR(200),
                data_consulta DATETIME,
                data_validade DATETIME,
                data_agendamento DATETIME,
                status VARCHAR(20) DEFAULT 'pendente',
                numero_certidao VARCHAR(100),
                arquivo_path VARCHAR(500),
                observacao TEXT,
                enviar_email BOOLEAN DEFAULT 0,
                pasta_destino VARCHAR(500),
                criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))

    # Cria tabelas novas se não existirem (create_all já faz isso, mas garantimos via migration)
    if "receita_historica" not in insp.get_table_names():
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS receita_historica (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                escritorio_id INTEGER NOT NULL REFERENCES escritorios(id),
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                competencia VARCHAR(7) NOT NULL,
                valor_receita FLOAT DEFAULT 0.0,
                origem VARCHAR(30) DEFAULT 'pgdas_d',
                criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))
    if "folha_mensal" not in insp.get_table_names():
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS folha_mensal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                escritorio_id INTEGER NOT NULL REFERENCES escritorios(id),
                cliente_id INTEGER NOT NULL REFERENCES clientes(id),
                competencia VARCHAR(7) NOT NULL,
                valor_salarios FLOAT DEFAULT 0.0,
                valor_pro_labore FLOAT DEFAULT 0.0,
                valor_inss_patronal FLOAT DEFAULT 0.0,
                valor_fgts FLOAT DEFAULT 0.0,
                valor_total FLOAT DEFAULT 0.0,
                origem VARCHAR(20) DEFAULT 'manual',
                observacao VARCHAR(300),
                criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))
    add_col("clientes", "ultimo_nsu_nfe",           "VARCHAR(20) DEFAULT '0'")
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
