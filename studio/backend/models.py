from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _agora():
    return datetime.now(timezone.utc)


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nome: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    senha_hash: Mapped[str] = mapped_column(String(200))
    dono: Mapped[bool] = mapped_column(Boolean, default=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_agora)

    identidade: Mapped["IdentidadeVisual"] = relationship(back_populates="usuario", uselist=False)


class IdentidadeVisual(Base):
    """A identidade visual + nicho que a IA segue em toda edição."""
    __tablename__ = "identidades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), unique=True)

    # Posicionamento
    nome_marca: Mapped[str] = mapped_column(String(120), default="Mendes e Lima Contabilidade")
    arroba: Mapped[str] = mapped_column(String(80), default="")
    nicho: Mapped[str] = mapped_column(Text, default=(
        "Contabilidade para o digital: infoprodutores, criadores de conteúdo, "
        "agências, experts e negócios online. Impostos, CNPJ, Fator R, pró-labore, "
        "Simples Nacional, sair da CLT para empreender."
    ))
    publico: Mapped[str] = mapped_column(Text, default=(
        "Empreendedores digitais que faturam com internet e querem pagar menos imposto "
        "de forma legal e organizar a vida financeira do negócio."
    ))
    tom_de_voz: Mapped[str] = mapped_column(Text, default="Didático, direto, próximo e confiável. Sem juridiquês.")
    cta_padrao: Mapped[str] = mapped_column(String(200), default="Me chama no direct e eu analiso o seu caso")
    instrucoes_fixas: Mapped[str] = mapped_column(Text, default=(
        "Corte seco nas pausas, fique sempre com a última tomada quando eu repetir uma frase, "
        "abra com um gancho forte na tela, legenda palavra por palavra e destaque os números "
        "e termos de impacto (imposto, economia, CNPJ, valores em R$)."
    ))

    # Visual
    cor_primaria: Mapped[str] = mapped_column(String(9), default="#0B2E4F")
    cor_destaque: Mapped[str] = mapped_column(String(9), default="#F5B700")
    cor_texto: Mapped[str] = mapped_column(String(9), default="#FFFFFF")
    fonte: Mapped[str] = mapped_column(String(60), default="Montserrat")
    estilo_legenda: Mapped[str] = mapped_column(String(20), default="curta")   # palavra | curta | linha
    posicao_legenda: Mapped[str] = mapped_column(String(20), default="centro")  # centro | baixo
    caixa_alta: Mapped[bool] = mapped_column(Boolean, default=True)
    logo_path: Mapped[str] = mapped_column(String(500), default="")
    logo_posicao: Mapped[str] = mapped_column(String(20), default="topo-direita")
    musica_path: Mapped[str] = mapped_column(String(500), default="")
    musica_volume: Mapped[float] = mapped_column(Float, default=0.10)

    # Edição
    formato_padrao: Mapped[str] = mapped_column(String(10), default="9:16")  # 9:16 | 1:1 | 4:5 | 16:9
    pausa_maxima: Mapped[float] = mapped_column(Float, default=0.35)  # segundos de silêncio tolerados
    zoom_automatico: Mapped[bool] = mapped_column(Boolean, default=True)
    aprovar_antes: Mapped[bool] = mapped_column(Boolean, default=False)  # mostra o plano antes de renderizar

    usuario: Mapped[Usuario] = relationship(back_populates="identidade")


class Projeto(Base):
    """Um vídeo: do bruto ao editado."""
    __tablename__ = "projetos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id"), index=True)
    titulo: Mapped[str] = mapped_column(String(200), default="Vídeo sem título")
    pedido: Mapped[str] = mapped_column(Text, default="")  # instrução livre em português
    formato: Mapped[str] = mapped_column(String(10), default="9:16")

    # enviado → transcrevendo → planejando → aguardando_aprovacao → renderizando → pronto | erro
    status: Mapped[str] = mapped_column(String(30), default="enviado", index=True)
    etapa: Mapped[str] = mapped_column(String(200), default="")
    progresso: Mapped[int] = mapped_column(Integer, default=0)
    erro: Mapped[str] = mapped_column(Text, default="")

    arquivo_bruto: Mapped[str] = mapped_column(String(500), default="")
    arquivo_final: Mapped[str] = mapped_column(String(500), default="")
    miniatura: Mapped[str] = mapped_column(String(500), default="")

    duracao_bruto: Mapped[float] = mapped_column(Float, default=0)
    duracao_final: Mapped[float] = mapped_column(Float, default=0)
    largura: Mapped[int] = mapped_column(Integer, default=0)
    altura: Mapped[int] = mapped_column(Integer, default=0)

    palavras: Mapped[list] = mapped_column(JSON, default=list)   # [{"w","ini","fim"}]
    plano: Mapped[dict] = mapped_column(JSON, default=dict)      # decisão da IA (ver diretor_ia.py)
    historico_pedidos: Mapped[list] = mapped_column(JSON, default=list)

    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_agora)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_agora, onupdate=_agora)
