from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, Text, ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional, List
import enum

from .database import Base


class PlanoEnum(str, enum.Enum):
    free = "free"          # 10 notas/mês
    starter = "starter"    # 100 notas/mês
    pro = "pro"            # 500 notas/mês
    enterprise = "enterprise"  # ilimitado


class RoleEnum(str, enum.Enum):
    admin = "admin"
    contador = "contador"


class ProviderNFSeEnum(str, enum.Enum):
    nacional = "nacional"    # NFS-e Nacional (SEFAZ)
    nfeio = "nfeio"          # NFe.io (agregadora)
    mock = "mock"            # Demo / desenvolvimento


class AnexoSimplesEnum(str, enum.Enum):
    I    = "I"     # Comércio
    II   = "II"    # Indústria
    III  = "III"   # Serviços — CPP inclusa, ISS menor (Fator R ≥ 28% ou atividade fixada)
    IV   = "IV"    # Serviços — CPP fora do DAS (construção, limpeza, vigilância)
    V    = "V"     # Serviços — CPP inclusa, maior tributação (Fator R < 28%)
    III_V = "III_V" # Serviços que oscilam entre III e V conforme Fator R


class TipoNotaEnum(str, enum.Enum):
    nfse = "nfse"    # Nota Fiscal de Serviço Eletrônica
    nfe = "nfe"      # Nota Fiscal Eletrônica (produtos, modelo 55)
    nfce = "nfce"    # Nota Fiscal do Consumidor Eletrônica (cupom, modelo 65)


class StatusNotaEnum(str, enum.Enum):
    pendente = "pendente"
    emitida = "emitida"
    cancelada = "cancelada"
    substituida = "substituida"
    erro = "erro"


# ---------------------------------------------------------------------------
# TENANT — Escritório Contábil
# ---------------------------------------------------------------------------

class Escritorio(Base):
    __tablename__ = "escritorios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    nome: Mapped[str] = mapped_column(String(200))
    cnpj: Mapped[str] = mapped_column(String(18), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    telefone: Mapped[Optional[str]] = mapped_column(String(20))
    crc: Mapped[Optional[str]] = mapped_column(String(30))

    plano: Mapped[PlanoEnum] = mapped_column(
        SAEnum(PlanoEnum), default=PlanoEnum.free
    )
    limite_notas_mes: Mapped[int] = mapped_column(Integer, default=2)
    notas_emitidas_mes: Mapped[int] = mapped_column(Integer, default=0)
    mes_referencia: Mapped[Optional[str]] = mapped_column(String(7))  # YYYY-MM

    # NFe.io — chave global do escritório (Company ID fica em cada Cliente)
    nfeio_api_key: Mapped[Optional[str]] = mapped_column(String(200))

    # Entrega de nota ao cliente
    nota_enviar_email: Mapped[bool] = mapped_column(Boolean, default=False)
    nota_pasta_destino: Mapped[Optional[str]] = mapped_column(String(500))
    smtp_host: Mapped[Optional[str]] = mapped_column(String(200))
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_usuario: Mapped[Optional[str]] = mapped_column(String(200))
    smtp_senha: Mapped[Optional[str]] = mapped_column(String(200))

    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    usuarios: Mapped[List["Usuario"]] = relationship(back_populates="escritorio")
    clientes: Mapped[List["Cliente"]] = relationship(back_populates="escritorio")
    notas: Mapped[List["Nota"]] = relationship(back_populates="escritorio")


# ---------------------------------------------------------------------------
# USUÁRIO — Contador / Admin do escritório
# ---------------------------------------------------------------------------

class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"))
    nome: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    senha_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[RoleEnum] = mapped_column(SAEnum(RoleEnum), default=RoleEnum.contador)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    escritorio: Mapped["Escritorio"] = relationship(back_populates="usuarios")


# ---------------------------------------------------------------------------
# CLIENTE — Empresa gerenciada pelo escritório
# ---------------------------------------------------------------------------

class Cliente(Base):
    __tablename__ = "clientes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"))

    razao_social: Mapped[str] = mapped_column(String(300))
    nome_fantasia: Mapped[Optional[str]] = mapped_column(String(300))
    cnpj: Mapped[str] = mapped_column(String(18), index=True)
    ie: Mapped[Optional[str]] = mapped_column(String(30))   # Inscrição Estadual
    im: Mapped[Optional[str]] = mapped_column(String(30))   # Inscrição Municipal

    regime_tributario: Mapped[Optional[str]] = mapped_column(String(30))
    optante_simples: Mapped[bool] = mapped_column(Boolean, default=False)

    # Endereço
    logradouro: Mapped[Optional[str]] = mapped_column(String(300))
    numero: Mapped[Optional[str]] = mapped_column(String(20))
    complemento: Mapped[Optional[str]] = mapped_column(String(100))
    bairro: Mapped[Optional[str]] = mapped_column(String(100))
    municipio: Mapped[Optional[str]] = mapped_column(String(100))
    codigo_ibge: Mapped[Optional[str]] = mapped_column(String(10))
    uf: Mapped[Optional[str]] = mapped_column(String(2))
    cep: Mapped[Optional[str]] = mapped_column(String(10))

    email: Mapped[Optional[str]] = mapped_column(String(200))
    telefone: Mapped[Optional[str]] = mapped_column(String(20))

    # Tipos de nota que o cliente emite
    emite_nfse: Mapped[bool] = mapped_column(Boolean, default=True)
    emite_nfe: Mapped[bool] = mapped_column(Boolean, default=False)

    # Configurações NFS-e
    nfse_provider: Mapped[ProviderNFSeEnum] = mapped_column(
        SAEnum(ProviderNFSeEnum), default=ProviderNFSeEnum.mock
    )
    nfse_api_key: Mapped[Optional[str]] = mapped_column(String(200))
    nfse_company_id: Mapped[Optional[str]] = mapped_column(String(100))
    nfse_municipio_codigo: Mapped[Optional[str]] = mapped_column(String(10))
    nfse_aliquota_iss: Mapped[Optional[float]] = mapped_column(Float, default=5.0)
    nfse_serie_rps: Mapped[Optional[str]] = mapped_column(String(5), default="RPS")
    nfse_ultimo_numero_rps: Mapped[int] = mapped_column(Integer, default=0)
    nfse_certificado_path: Mapped[Optional[str]] = mapped_column(String(500))
    nfse_certificado_senha: Mapped[Optional[str]] = mapped_column(String(200))
    nfse_certificado_vencimento: Mapped[Optional[datetime]] = mapped_column(DateTime)
    nfe_certificado_vencimento: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Simples Nacional — Anexo e Fator R
    anexo_simples: Mapped[Optional[str]] = mapped_column(String(10))  # I II III IV V III_V
    atividade_permite_fator_r: Mapped[bool] = mapped_column(Boolean, default=False)
    limite_simples: Mapped[float] = mapped_column(Float, default=4_800_000.0)

    # Configurações NF-e
    nfe_provider: Mapped[Optional[str]] = mapped_column(String(20), default="mock")  # mock/nfeio/sefaz
    nfe_api_key: Mapped[Optional[str]] = mapped_column(String(200))   # NFe.io key para NF-e
    nfe_company_id: Mapped[Optional[str]] = mapped_column(String(100)) # NFe.io company para NF-e
    nfe_serie: Mapped[Optional[str]] = mapped_column(String(5), default="1")
    nfe_ultimo_numero: Mapped[int] = mapped_column(Integer, default=0)
    nfe_certificado_path: Mapped[Optional[str]] = mapped_column(String(500))
    nfe_certificado_senha: Mapped[Optional[str]] = mapped_column(String(200))
    ultimo_nsu_nfe: Mapped[Optional[str]] = mapped_column(String(20), default="0")

    # Configurações Boleto (opcional)
    boleto_ativo: Mapped[bool] = mapped_column(Boolean, default=False)
    boleto_provider: Mapped[Optional[str]] = mapped_column(String(20))  # asaas/gerencianet/mock
    boleto_api_key: Mapped[Optional[str]] = mapped_column(String(200))
    boleto_dias_vencimento: Mapped[int] = mapped_column(Integer, default=3)  # dias após emissão

    # CNAE (preenchido via busca RFB)
    cnae: Mapped[Optional[str]] = mapped_column(String(10))
    cnae_descricao: Mapped[Optional[str]] = mapped_column(String(300))

    # Responsável (funcionário do escritório) — carteira de clientes
    responsavel_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("usuarios.id"), nullable=True)

    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    escritorio: Mapped["Escritorio"] = relationship(back_populates="clientes")
    notas: Mapped[List["Nota"]] = relationship(back_populates="cliente")


# ---------------------------------------------------------------------------
# NOTA FISCAL — NFS-e ou NF-e
# ---------------------------------------------------------------------------

class Nota(Base):
    __tablename__ = "notas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"))
    cliente_id: Mapped[int] = mapped_column(Integer, ForeignKey("clientes.id"))

    tipo: Mapped[TipoNotaEnum] = mapped_column(SAEnum(TipoNotaEnum))
    status: Mapped[StatusNotaEnum] = mapped_column(
        SAEnum(StatusNotaEnum), default=StatusNotaEnum.pendente
    )

    numero: Mapped[Optional[str]] = mapped_column(String(30))
    serie: Mapped[Optional[str]] = mapped_column(String(10))
    numero_rps: Mapped[Optional[int]] = mapped_column(Integer)
    chave_acesso: Mapped[Optional[str]] = mapped_column(String(50))

    data_emissao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    data_competencia: Mapped[Optional[str]] = mapped_column(String(7))  # YYYY-MM

    # Tomador do serviço / destinatário
    tomador_cpf_cnpj: Mapped[Optional[str]] = mapped_column(String(20))
    tomador_razao_social: Mapped[Optional[str]] = mapped_column(String(300))
    tomador_email: Mapped[Optional[str]] = mapped_column(String(200))
    tomador_logradouro: Mapped[Optional[str]] = mapped_column(String(300))
    tomador_numero: Mapped[Optional[str]] = mapped_column(String(20))
    tomador_complemento: Mapped[Optional[str]] = mapped_column(String(100))
    tomador_bairro: Mapped[Optional[str]] = mapped_column(String(100))
    tomador_municipio: Mapped[Optional[str]] = mapped_column(String(100))
    tomador_codigo_ibge: Mapped[Optional[str]] = mapped_column(String(10))
    tomador_uf: Mapped[Optional[str]] = mapped_column(String(2))
    tomador_cep: Mapped[Optional[str]] = mapped_column(String(10))

    # Valores
    valor_servico: Mapped[Optional[float]] = mapped_column(Float)
    valor_deducoes: Mapped[Optional[float]] = mapped_column(Float, default=0)
    aliquota_iss: Mapped[Optional[float]] = mapped_column(Float)
    valor_iss: Mapped[Optional[float]] = mapped_column(Float)
    valor_pis: Mapped[Optional[float]] = mapped_column(Float, default=0)
    valor_cofins: Mapped[Optional[float]] = mapped_column(Float, default=0)
    valor_inss: Mapped[Optional[float]] = mapped_column(Float, default=0)
    valor_ir: Mapped[Optional[float]] = mapped_column(Float, default=0)
    valor_csll: Mapped[Optional[float]] = mapped_column(Float, default=0)
    valor_liquido: Mapped[Optional[float]] = mapped_column(Float)

    # NFS-e
    codigo_servico_lc116: Mapped[Optional[str]] = mapped_column(String(20))
    codigo_servico_municipal: Mapped[Optional[str]] = mapped_column(String(20))
    discriminacao: Mapped[Optional[Text]] = mapped_column(Text)
    iss_retido: Mapped[bool] = mapped_column(Boolean, default=False)

    # NF-e
    natureza_operacao: Mapped[Optional[str]] = mapped_column(String(100))
    cfop: Mapped[Optional[str]] = mapped_column(String(10))

    # Resposta do provedor
    provider: Mapped[Optional[str]] = mapped_column(String(30))
    provider_id: Mapped[Optional[str]] = mapped_column(String(100))
    provider_status: Mapped[Optional[str]] = mapped_column(String(50))

    xml_content: Mapped[Optional[Text]] = mapped_column(Text)
    pdf_base64: Mapped[Optional[Text]] = mapped_column(Text)
    link_pdf: Mapped[Optional[str]] = mapped_column(String(500))
    link_xml: Mapped[Optional[str]] = mapped_column(String(500))

    # Cancelamento / Substituição
    motivo_cancelamento: Mapped[Optional[str]] = mapped_column(Text)
    data_cancelamento: Mapped[Optional[datetime]] = mapped_column(DateTime)
    nota_substituta_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("notas.id"), nullable=True)
    nota_substituida_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("notas.id"), nullable=True)

    # Boleto vinculado
    boleto_url: Mapped[Optional[str]] = mapped_column(String(500))
    boleto_linha_digitavel: Mapped[Optional[str]] = mapped_column(String(120))
    boleto_codigo_barras: Mapped[Optional[str]] = mapped_column(String(60))
    boleto_vencimento: Mapped[Optional[datetime]] = mapped_column(DateTime)
    boleto_status: Mapped[Optional[str]] = mapped_column(String(20))  # pendente/pago/vencido/cancelado

    erro_mensagem: Mapped[Optional[Text]] = mapped_column(Text)

    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    escritorio: Mapped["Escritorio"] = relationship(back_populates="notas")
    cliente: Mapped["Cliente"] = relationship(back_populates="notas")
    itens: Mapped[List["ItemNota"]] = relationship(back_populates="nota")


# ---------------------------------------------------------------------------
# ITEM NF-e
# ---------------------------------------------------------------------------

class DocumentoRecebido(Base):
    """NF-e ou NFS-e recebida pelo cliente (monitoramento automático + upload manual)."""
    __tablename__ = "documentos_recebidos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"))
    cliente_id: Mapped[int] = mapped_column(Integer, ForeignKey("clientes.id"))

    tipo: Mapped[TipoNotaEnum] = mapped_column(SAEnum(TipoNotaEnum))

    # Como chegou: 'manual' (upload XML), 'sefaz_dfe' (SEFAZ DF-e auto), 'nfeio' (webhook)
    origem: Mapped[str] = mapped_column(String(20), default="manual")

    chave_acesso: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    numero: Mapped[Optional[str]] = mapped_column(String(30))
    serie: Mapped[Optional[str]] = mapped_column(String(10))

    data_emissao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    data_entrada: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Quem emitiu (fornecedor/prestador)
    emitente_cnpj: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    emitente_razao_social: Mapped[Optional[str]] = mapped_column(String(300))
    emitente_uf: Mapped[Optional[str]] = mapped_column(String(2))
    emitente_municipio: Mapped[Optional[str]] = mapped_column(String(100))

    valor_total: Mapped[Optional[float]] = mapped_column(Float)

    # NF-e: manifestação do destinatário
    # pendente, ciencia_operacao, confirmacao_operacao,
    # desconhecimento, operacao_nao_realizada
    status_manifestacao: Mapped[str] = mapped_column(String(40), default="pendente")

    natureza_operacao: Mapped[Optional[str]] = mapped_column(String(100))

    # Conteúdo XML original
    xml_content: Mapped[Optional[Text]] = mapped_column(Text)
    observacoes: Mapped[Optional[Text]] = mapped_column(Text)

    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    cliente: Mapped["Cliente"] = relationship()
    escritorio: Mapped["Escritorio"] = relationship()


# ---------------------------------------------------------------------------
# FORNECEDOR — Emitentes de notas recebidas (auto-cadastrado ou manual)
# ---------------------------------------------------------------------------

class Fornecedor(Base):
    """Fornecedor/prestador de serviço identificado nas notas recebidas."""
    __tablename__ = "fornecedores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"), index=True)

    cnpj: Mapped[str] = mapped_column(String(18), index=True)
    razao_social: Mapped[str] = mapped_column(String(300))
    nome_fantasia: Mapped[Optional[str]] = mapped_column(String(300))
    uf: Mapped[Optional[str]] = mapped_column(String(2))
    municipio: Mapped[Optional[str]] = mapped_column(String(100))
    email: Mapped[Optional[str]] = mapped_column(String(200))
    telefone: Mapped[Optional[str]] = mapped_column(String(30))
    categoria: Mapped[Optional[str]] = mapped_column(String(100))
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    total_notas: Mapped[int] = mapped_column(Integer, default=0)
    valor_total_notas: Mapped[float] = mapped_column(Float, default=0.0)
    ultima_nota_em: Mapped[Optional[datetime]] = mapped_column(DateTime)
    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    escritorio: Mapped["Escritorio"] = relationship()


# ---------------------------------------------------------------------------
# CONTRATO — Faturamento Recorrente (NFS-e ou NF-e de valor fixo mensal)
# ---------------------------------------------------------------------------

class ModoEmissaoEnum(str, enum.Enum):
    automatico = "automatico"   # emite no dia sem intervenção
    fila = "fila"               # coloca na fila para o contador aprovar


class Contrato(Base):
    """Contrato de serviço com faturamento recorrente mensal."""
    __tablename__ = "contratos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"))
    cliente_id: Mapped[int] = mapped_column(Integer, ForeignKey("clientes.id"))

    descricao: Mapped[str] = mapped_column(String(300))
    tipo_nota: Mapped[TipoNotaEnum] = mapped_column(SAEnum(TipoNotaEnum), default=TipoNotaEnum.nfse)

    # Valor e tributação
    valor: Mapped[float] = mapped_column(Float)
    aliquota_iss: Mapped[Optional[float]] = mapped_column(Float, default=5.0)
    iss_retido: Mapped[bool] = mapped_column(Boolean, default=False)
    valor_deducoes: Mapped[float] = mapped_column(Float, default=0)
    valor_pis: Mapped[float] = mapped_column(Float, default=0)
    valor_cofins: Mapped[float] = mapped_column(Float, default=0)
    valor_inss: Mapped[float] = mapped_column(Float, default=0)
    valor_ir: Mapped[float] = mapped_column(Float, default=0)
    valor_csll: Mapped[float] = mapped_column(Float, default=0)

    # Serviço
    codigo_servico_lc116: Mapped[Optional[str]] = mapped_column(String(10))
    codigo_servico_municipal: Mapped[Optional[str]] = mapped_column(String(20))
    discriminacao: Mapped[Optional[str]] = mapped_column(Text)

    # Dados de Obra (construtoras)
    is_construtora: Mapped[bool] = mapped_column(Boolean, default=False)
    obra_cei: Mapped[Optional[str]] = mapped_column(String(30))
    obra_art: Mapped[Optional[str]] = mapped_column(String(50))
    obra_alvara: Mapped[Optional[str]] = mapped_column(String(50))
    obra_matricula: Mapped[Optional[str]] = mapped_column(String(50))
    obra_endereco: Mapped[Optional[str]] = mapped_column(String(300))
    obra_inss_aliquota: Mapped[float] = mapped_column(Float, default=0.0)

    # Tomador pré-configurado
    tomador_cpf_cnpj: Mapped[Optional[str]] = mapped_column(String(18))
    tomador_razao_social: Mapped[Optional[str]] = mapped_column(String(300))
    tomador_email: Mapped[Optional[str]] = mapped_column(String(200))
    tomador_logradouro: Mapped[Optional[str]] = mapped_column(String(300))
    tomador_numero: Mapped[Optional[str]] = mapped_column(String(20))
    tomador_complemento: Mapped[Optional[str]] = mapped_column(String(100))
    tomador_bairro: Mapped[Optional[str]] = mapped_column(String(100))
    tomador_municipio: Mapped[Optional[str]] = mapped_column(String(100))
    tomador_codigo_ibge: Mapped[Optional[str]] = mapped_column(String(10))
    tomador_uf: Mapped[Optional[str]] = mapped_column(String(2))
    tomador_cep: Mapped[Optional[str]] = mapped_column(String(10))

    # Agendamento
    dia_emissao: Mapped[int] = mapped_column(Integer, default=1)      # 1–28
    modo_emissao: Mapped[ModoEmissaoEnum] = mapped_column(
        SAEnum(ModoEmissaoEnum), default=ModoEmissaoEnum.fila
    )
    gerar_boleto: Mapped[bool] = mapped_column(Boolean, default=False)

    # Controle
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    proxima_emissao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    ultima_nota_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("notas.id"), nullable=True
    )
    total_emitido: Mapped[int] = mapped_column(Integer, default=0)

    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    escritorio: Mapped["Escritorio"] = relationship()
    cliente: Mapped["Cliente"] = relationship()


# ---------------------------------------------------------------------------
# RECEITA HISTÓRICA — Lançamento manual para meses anteriores ao Limio
# ---------------------------------------------------------------------------

class ReceitaHistorica(Base):
    """Receita bruta mensal lançada manualmente para meses sem notas no Limio.
    Usada no cálculo do RBT12 e Fator R. Fonte: PGDAS-D do cliente."""
    __tablename__ = "receita_historica"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"))
    cliente_id: Mapped[int] = mapped_column(Integer, ForeignKey("clientes.id"), index=True)
    competencia: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM

    valor_receita: Mapped[float] = mapped_column(Float, default=0.0)
    # Fonte de onde veio o número (para auditoria)
    origem: Mapped[str] = mapped_column(String(30), default="pgdas_d")  # pgdas_d / manual / nota_fiscal

    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    cliente: Mapped["Cliente"] = relationship()
    escritorio: Mapped["Escritorio"] = relationship()


# ---------------------------------------------------------------------------
# FOLHA MENSAL — Lançamento pelo contador (base para Fator R)
# ---------------------------------------------------------------------------

class FolhaMensal(Base):
    """Valores de folha de pagamento por competência, lançados manualmente pelo contador.
    Usados para calcular o Fator R (Folha 12m / RBT12) nas atividades dos Anexos III/V."""
    __tablename__ = "folha_mensal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"))
    cliente_id: Mapped[int] = mapped_column(Integer, ForeignKey("clientes.id"), index=True)
    competencia: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM

    valor_salarios: Mapped[float] = mapped_column(Float, default=0.0)
    valor_pro_labore: Mapped[float] = mapped_column(Float, default=0.0)
    valor_inss_patronal: Mapped[float] = mapped_column(Float, default=0.0)
    valor_fgts: Mapped[float] = mapped_column(Float, default=0.0)
    valor_total: Mapped[float] = mapped_column(Float, default=0.0)  # soma dos anteriores

    # "manual" = lançado pelo contador | "esocial" = importado via DCTFWeb/eSocial no futuro
    origem: Mapped[str] = mapped_column(String(20), default="manual")
    observacao: Mapped[Optional[str]] = mapped_column(String(300))

    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    cliente: Mapped["Cliente"] = relationship()
    escritorio: Mapped["Escritorio"] = relationship()


# ---------------------------------------------------------------------------
# ICMS MENSAL — Crédito, débito e saldo por competência
# ---------------------------------------------------------------------------

class IcmsMensal(Base):
    """ICMS crédito/débito por competência — lançamento manual pelo contador."""
    __tablename__ = "icms_mensal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"))
    cliente_id: Mapped[int] = mapped_column(Integer, ForeignKey("clientes.id"), index=True)
    competencia: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM

    credito: Mapped[float] = mapped_column(Float, default=0.0)   # ICMS sobre compras
    debito: Mapped[float] = mapped_column(Float, default=0.0)    # ICMS sobre vendas
    saldo: Mapped[float] = mapped_column(Float, default=0.0)     # débito − crédito (>0 = a recolher)

    origem: Mapped[str] = mapped_column(String(20), default="manual")
    observacao: Mapped[Optional[str]] = mapped_column(String(300))

    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    cliente: Mapped["Cliente"] = relationship()
    escritorio: Mapped["Escritorio"] = relationship()


# ---------------------------------------------------------------------------
# CERTIDÕES — Gestão de certidões negativas dos clientes
# ---------------------------------------------------------------------------

class Certidao(Base):
    """Registro de certidão (CND, CNDT, FGTS etc.) por cliente."""
    __tablename__ = "certidoes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    escritorio_id: Mapped[int] = mapped_column(Integer, ForeignKey("escritorios.id"))
    cliente_id: Mapped[int] = mapped_column(Integer, ForeignKey("clientes.id"), index=True)

    # Tipo de certidão
    tipo: Mapped[str] = mapped_column(String(30), index=True)
    # cnd_federal, cnd_falencia, cnd_fgts, cndt_tst, cndt_trt,
    # cnd_estadual, cnd_estadual_nc, cnd_municipal

    nome_descritivo: Mapped[Optional[str]] = mapped_column(String(200))
    data_consulta: Mapped[Optional[datetime]] = mapped_column(DateTime)
    data_validade: Mapped[Optional[datetime]] = mapped_column(DateTime)
    data_agendamento: Mapped[Optional[datetime]] = mapped_column(DateTime)  # próxima verificação

    # regular, irregular, pendente, em_analise, vencida
    status: Mapped[str] = mapped_column(String(20), default="pendente")

    numero_certidao: Mapped[Optional[str]] = mapped_column(String(100))
    arquivo_path: Mapped[Optional[str]] = mapped_column(String(500))
    observacao: Mapped[Optional[str]] = mapped_column(Text)

    # Entrega automática
    enviar_email: Mapped[bool] = mapped_column(Boolean, default=False)
    pasta_destino: Mapped[Optional[str]] = mapped_column(String(500))

    criado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    cliente: Mapped["Cliente"] = relationship()
    escritorio: Mapped["Escritorio"] = relationship()


class ItemNota(Base):
    __tablename__ = "itens_nota"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    nota_id: Mapped[int] = mapped_column(Integer, ForeignKey("notas.id"))
    numero_item: Mapped[int] = mapped_column(Integer)
    codigo_produto: Mapped[Optional[str]] = mapped_column(String(60))
    descricao: Mapped[str] = mapped_column(String(500))
    ncm: Mapped[Optional[str]] = mapped_column(String(10))
    cfop: Mapped[Optional[str]] = mapped_column(String(10))
    unidade: Mapped[Optional[str]] = mapped_column(String(10))
    quantidade: Mapped[float] = mapped_column(Float, default=1)
    valor_unitario: Mapped[float] = mapped_column(Float)
    valor_total: Mapped[float] = mapped_column(Float)
    aliquota_icms: Mapped[Optional[float]] = mapped_column(Float)
    valor_icms: Mapped[Optional[float]] = mapped_column(Float)
    aliquota_pis: Mapped[Optional[float]] = mapped_column(Float)
    valor_pis: Mapped[Optional[float]] = mapped_column(Float)
    aliquota_cofins: Mapped[Optional[float]] = mapped_column(Float)
    valor_cofins: Mapped[Optional[float]] = mapped_column(Float)

    nota: Mapped["Nota"] = relationship(back_populates="itens")
