from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime
from .models import PlanoEnum, RoleEnum, ProviderNFSeEnum, TipoNotaEnum, StatusNotaEnum


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class EscritorioCreate(BaseModel):
    nome: str
    cnpj: str
    email: EmailStr
    telefone: Optional[str] = None
    crc: Optional[str] = None
    # Primeiro usuário (admin)
    usuario_nome: str
    usuario_email: EmailStr
    senha: str

    @field_validator("cnpj")
    @classmethod
    def cnpj_only_digits_or_formatted(cls, v: str) -> str:
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) != 14:
            raise ValueError("CNPJ deve ter 14 dígitos")
        return digits


class LoginRequest(BaseModel):
    email: EmailStr
    senha: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    usuario_nome: str
    escritorio_nome: str
    escritorio_id: int
    plano: PlanoEnum
    role: RoleEnum


class UsuarioResponse(BaseModel):
    id: int
    nome: str
    email: str
    role: RoleEnum
    ativo: bool

    model_config = {"from_attributes": True}


class EscritorioResponse(BaseModel):
    id: int
    nome: str
    cnpj: str
    email: str
    telefone: Optional[str]
    crc: Optional[str]
    plano: PlanoEnum
    limite_notas_mes: int
    notas_emitidas_mes: int
    ativo: bool
    criado_em: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


class ClienteCreate(BaseModel):
    razao_social: str
    nome_fantasia: Optional[str] = None
    cnpj: str
    ie: Optional[str] = None
    im: Optional[str] = None
    regime_tributario: Optional[str] = "Simples Nacional"
    optante_simples: bool = False
    logradouro: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    municipio: Optional[str] = None
    codigo_ibge: Optional[str] = None
    uf: Optional[str] = None
    cep: Optional[str] = None
    email: Optional[str] = None
    telefone: Optional[str] = None
    emite_nfse: bool = True
    nfse_provider: ProviderNFSeEnum = ProviderNFSeEnum.mock
    nfse_api_key: Optional[str] = None
    nfse_company_id: Optional[str] = None
    nfse_municipio_codigo: Optional[str] = None
    nfse_aliquota_iss: float = 5.0
    nfse_serie_rps: Optional[str] = "RPS"
    emite_nfe: bool = False
    nfe_provider: Optional[str] = "mock"
    nfe_api_key: Optional[str] = None
    nfe_company_id: Optional[str] = None
    nfe_serie: Optional[str] = "1"
    nfe_certificado_path: Optional[str] = None
    nfe_certificado_senha: Optional[str] = None
    boleto_ativo: bool = False
    boleto_provider: Optional[str] = "mock"
    boleto_api_key: Optional[str] = None
    boleto_dias_vencimento: int = 3

    @field_validator("cnpj")
    @classmethod
    def cnpj_format(cls, v: str) -> str:
        return "".join(c for c in v if c.isdigit())


class ClienteUpdate(BaseModel):
    razao_social: Optional[str] = None
    nome_fantasia: Optional[str] = None
    ie: Optional[str] = None
    im: Optional[str] = None
    regime_tributario: Optional[str] = None
    optante_simples: Optional[bool] = None
    logradouro: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    municipio: Optional[str] = None
    codigo_ibge: Optional[str] = None
    uf: Optional[str] = None
    cep: Optional[str] = None
    email: Optional[str] = None
    telefone: Optional[str] = None
    emite_nfse: Optional[bool] = None
    nfse_provider: Optional[ProviderNFSeEnum] = None
    nfse_api_key: Optional[str] = None
    nfse_company_id: Optional[str] = None
    nfse_municipio_codigo: Optional[str] = None
    nfse_aliquota_iss: Optional[float] = None
    nfse_serie_rps: Optional[str] = None
    emite_nfe: Optional[bool] = None
    nfe_provider: Optional[str] = None
    nfe_api_key: Optional[str] = None
    nfe_company_id: Optional[str] = None
    nfe_serie: Optional[str] = None
    nfe_certificado_path: Optional[str] = None
    nfe_certificado_senha: Optional[str] = None
    boleto_ativo: Optional[bool] = None
    boleto_provider: Optional[str] = None
    boleto_api_key: Optional[str] = None
    boleto_dias_vencimento: Optional[int] = None


class ClienteResponse(BaseModel):
    id: int
    escritorio_id: int
    razao_social: str
    nome_fantasia: Optional[str] = None
    cnpj: str
    ie: Optional[str] = None
    im: Optional[str] = None
    regime_tributario: Optional[str] = None
    optante_simples: bool = False
    logradouro: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    municipio: Optional[str] = None
    codigo_ibge: Optional[str] = None
    uf: Optional[str] = None
    cep: Optional[str] = None
    email: Optional[str] = None
    telefone: Optional[str] = None
    emite_nfse: bool = True
    nfse_provider: ProviderNFSeEnum = ProviderNFSeEnum.mock
    nfse_api_key: Optional[str] = None
    nfse_company_id: Optional[str] = None
    nfse_municipio_codigo: Optional[str] = None
    nfse_aliquota_iss: Optional[float] = 5.0
    nfse_serie_rps: Optional[str] = "RPS"
    nfse_ultimo_numero_rps: int = 0
    emite_nfe: bool = False
    nfe_provider: Optional[str] = "mock"
    nfe_api_key: Optional[str] = None
    nfe_company_id: Optional[str] = None
    nfe_serie: Optional[str] = "1"
    nfe_ultimo_numero: int = 0
    nfe_certificado_path: Optional[str] = None
    boleto_ativo: bool = False
    boleto_provider: Optional[str] = None
    boleto_api_key: Optional[str] = None
    boleto_dias_vencimento: int = 3
    ativo: bool = True
    criado_em: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Tomador (shared entre NFS-e e NF-e)
# ---------------------------------------------------------------------------

class TomadorSchema(BaseModel):
    cpf_cnpj: str
    razao_social: str
    email: Optional[str] = None
    logradouro: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    municipio: Optional[str] = None
    codigo_ibge: Optional[str] = None
    uf: Optional[str] = None
    cep: Optional[str] = None


# ---------------------------------------------------------------------------
# NFS-e
# ---------------------------------------------------------------------------

class EmitirNFSeRequest(BaseModel):
    cliente_id: int
    tomador: TomadorSchema
    data_competencia: str  # YYYY-MM
    discriminacao: str
    valor_servico: float
    codigo_servico_lc116: str  # ex: "1.05.01"
    codigo_servico_municipal: Optional[str] = None
    aliquota_iss: Optional[float] = None  # usa padrão do cliente se omitido
    iss_retido: bool = False
    valor_deducoes: float = 0.0
    valor_pis: float = 0.0
    valor_cofins: float = 0.0
    valor_inss: float = 0.0
    valor_ir: float = 0.0
    valor_csll: float = 0.0


class CancelarNotaRequest(BaseModel):
    motivo: Optional[str] = None


# ---------------------------------------------------------------------------
# NF-e
# ---------------------------------------------------------------------------

class ItemNFeSchema(BaseModel):
    descricao: str
    codigo_produto: Optional[str] = None
    ncm: str
    cfop: str
    unidade: str = "UN"
    quantidade: float = 1.0
    valor_unitario: float
    aliquota_icms: float = 0.0
    aliquota_pis: float = 0.65
    aliquota_cofins: float = 3.0


class EmitirNFeRequest(BaseModel):
    cliente_id: int
    tomador: TomadorSchema
    natureza_operacao: str = "Venda de mercadorias"
    data_competencia: str  # YYYY-MM
    itens: List[ItemNFeSchema]
    informacoes_adicionais: Optional[str] = None


# ---------------------------------------------------------------------------
# Nota (response)
# ---------------------------------------------------------------------------

class NotaResponse(BaseModel):
    id: int
    escritorio_id: int
    cliente_id: int
    cliente_razao_social: Optional[str] = None
    tipo: TipoNotaEnum
    status: StatusNotaEnum
    numero: Optional[str]
    serie: Optional[str]
    numero_rps: Optional[int]
    chave_acesso: Optional[str]
    data_emissao: Optional[datetime]
    data_competencia: Optional[str]
    tomador_cpf_cnpj: Optional[str]
    tomador_razao_social: Optional[str]
    valor_servico: Optional[float]
    valor_liquido: Optional[float]
    valor_iss: Optional[float]
    aliquota_iss: Optional[float]
    iss_retido: bool
    discriminacao: Optional[str]
    codigo_servico_lc116: Optional[str]
    provider: Optional[str]
    provider_id: Optional[str]
    provider_status: Optional[str]
    link_pdf: Optional[str]
    link_xml: Optional[str]
    erro_mensagem: Optional[str]
    motivo_cancelamento: Optional[str]
    nota_substituida_id: Optional[int] = None
    boleto_url: Optional[str] = None
    boleto_linha_digitavel: Optional[str] = None
    boleto_codigo_barras: Optional[str] = None
    boleto_vencimento: Optional[datetime] = None
    boleto_status: Optional[str] = None
    criado_em: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardResponse(BaseModel):
    escritorio: EscritorioResponse
    total_clientes: int
    notas_hoje: int
    notas_mes: int
    valor_mes: float
    limite_mes: int
    percentual_uso: float
    ultimas_notas: List[NotaResponse]
