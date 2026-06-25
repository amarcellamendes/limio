"""
Integrações com Receita Federal (PGDAS-D) e eSocial via certificado A1.

Fluxo:
  1. Carrega o .pfx do cliente (já salvo em DATA_DIR/certs/...)
  2. Extrai chave privada e certificado em PEM
  3. Usa httpx com SSL client certificate para autenticação mTLS
  4. Faz a requisição ao portal da Receita / eSocial
  5. Parseia a resposta e retorna dados estruturados

Limitação: O portal do PGDAS-D e o eSocial exigem, além do mTLS,
sessão web com cookies e/ou assinatura XML (para eSocial). O endpoint
atual faz a conexão autenticada — se o portal retornar dados em HTML
estático, o parser extrai o que conseguir; caso contrário, retorna
status="conexao_ok" para que o contador preencha manualmente.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
import tempfile, os, ssl

from ..database import get_db
from ..models import Cliente, Escritorio
from ..auth import get_escritorio_atual
from ..config import settings

router = APIRouter(prefix="/api/integracoes", tags=["Integrações RF/eSocial"])


# URLs dos portais da Receita Federal e eSocial
_PGDAS_URL = "https://www8.receita.fazenda.gov.br/SimplesNacional/Aplicacoes/ATBHE/pgdas.app/emPGDAS/"
_ESOCIAL_URL = "https://webservices.consulta.esocial.gov.br/WsConsultaESocial/ServicoConsultaESocial.svc"


async def _carregar_certificado(cliente: Cliente, tipo: str, senha_override: str = ""):
    """Extrai PEM (cert + key) do .pfx do cliente. Retorna (cert_pem, key_pem, erro_str)."""
    path = getattr(cliente, f"{tipo}_certificado_path", None)
    senha = senha_override or getattr(cliente, f"{tipo}_certificado_senha", None) or ""

    if not path:
        return None, None, f"Certificado {tipo.upper()} não cadastrado. Faça o upload no cadastro do cliente."
    if not os.path.exists(path):
        return None, None, (
            f"Arquivo do certificado {tipo.upper()} não encontrado no servidor ({path}). "
            "Faça o upload novamente no cadastro do cliente."
        )

    try:
        from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12
        from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

        with open(path, "rb") as f:
            pfx_bytes = f.read()

        p12 = load_pkcs12(pfx_bytes, senha.encode("utf-8") if senha else b"")
        cert_pem = p12.cert.certificate.public_bytes(Encoding.PEM)
        key_pem = p12.key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        return cert_pem, key_pem, None
    except Exception as e:
        msg = str(e).lower()
        if "mac" in msg or "password" in msg or "invalid" in msg or "decrypt" in msg:
            dica = (
                f"Senha incorreta para o certificado {tipo.upper()}. "
                "Corrija a senha no cadastro do cliente e tente novamente."
            )
        else:
            dica = f"Erro ao abrir certificado {tipo.upper()}: {str(e)}"
        return None, None, dica


@router.post("/{cliente_id}/buscar-pgdas")
async def buscar_pgdas(
    cliente_id: int,
    ano: int = Query(default=2025),
    senha: str = Query(default=""),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """
    Busca histórico de receita bruta no PGDAS-D usando o certificado A1 do cliente.
    Retorna lista de competências com valor de receita, ou status de conexão.
    """
    result = await db.execute(
        select(Cliente).where(Cliente.id == cliente_id, Cliente.escritorio_id == escritorio.id)
    )
    cliente = result.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")

    # Tenta NFS-e primeiro, depois NF-e (ambos com senha_override se fornecida)
    cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfse", senha)
    if erro:
        cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfe", senha)
    if erro:
        raise HTTPException(400, erro)

    try:
        import httpx

        # Grava certs em arquivos temporários para o SSL context do httpx
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cf:
            cf.write(cert_pem)
            cert_path = cf.name
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
            kf.write(key_pem)
            key_path = kf.name

        try:
            async with httpx.AsyncClient(
                cert=(cert_path, key_path),
                verify=True,
                timeout=15,
                follow_redirects=True,
            ) as http:
                resp = await http.get(
                    _PGDAS_URL,
                    params={"cnpj": cliente.cnpj.replace(".", "").replace("/", "").replace("-", ""), "ano": ano},
                    headers={"User-Agent": "Mozilla/5.0 (Limio/1.0)"},
                )

            status_code = resp.status_code
            body = resp.text

            # Tenta extrair dados da resposta HTML
            receitas = _parse_pgdas_html(body, ano)

            return {
                "status": "ok",
                "status_http": status_code,
                "receitas": receitas,
                "aviso": (
                    "Dados extraídos com sucesso." if receitas
                    else "Conexão estabelecida com a Receita Federal, mas o portal retornou página sem dados tabulares. "
                         "Isso pode ocorrer porque o portal exige navegação com JavaScript. "
                         "Lance os valores manualmente usando o extrato do PGDAS-D."
                ),
            }
        finally:
            os.unlink(cert_path)
            os.unlink(key_path)

    except Exception as e:
        msg = str(e)
        if "SSL" in msg or "certificate" in msg.lower():
            raise HTTPException(502, f"Erro de certificado SSL: {msg}")
        if "ConnectError" in msg or "timeout" in msg.lower():
            raise HTTPException(502, "Não foi possível conectar à Receita Federal. Tente novamente em alguns instantes.")
        raise HTTPException(502, f"Erro ao acessar PGDAS-D: {msg}")


@router.post("/{cliente_id}/buscar-esocial")
async def buscar_esocial(
    cliente_id: int,
    ano: int = Query(default=2025),
    senha: str = Query(default=""),
    escritorio: Escritorio = Depends(get_escritorio_atual),
    db: AsyncSession = Depends(get_db),
):
    """
    Consulta eventos S-1200 (Remuneração) no eSocial via certificado A1.
    Retorna valores de folha por competência, ou status de conexão.
    """
    result = await db.execute(
        select(Cliente).where(Cliente.id == cliente_id, Cliente.escritorio_id == escritorio.id)
    )
    cliente = result.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado.")

    cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfse", senha)
    if erro:
        cert_pem, key_pem, erro = await _carregar_certificado(cliente, "nfe", senha)
    if erro:
        raise HTTPException(400, erro)

    cnpj = cliente.cnpj.replace(".", "").replace("/", "").replace("-", "")

    soap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:v1="http://www.esocial.gov.br/servicos/consulta/loteeventos/empregador/pj/envio/v1_1_0">
  <soapenv:Header/>
  <soapenv:Body>
    <v1:ConsultarLoteEventos>
      <v1:consulta>
        <eSocial xmlns="http://www.esocial.gov.br/schema/lote/eventos/envio/v1_1_1">
          <envioLoteEventos grupo="1">
            <ideEmpregador>
              <tpInsc>1</tpInsc>
              <nrInsc>{cnpj[:8]}</nrInsc>
            </ideEmpregador>
            <ideTransmissor>
              <tpInsc>1</tpInsc>
              <nrInsc>{cnpj[:8]}</nrInsc>
            </ideTransmissor>
          </envioLoteEventos>
        </eSocial>
      </v1:consulta>
    </v1:ConsultarLoteEventos>
  </soapenv:Body>
</soapenv:Envelope>"""

    try:
        import httpx

        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cf:
            cf.write(cert_pem)
            cert_path = cf.name
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
            kf.write(key_pem)
            key_path = kf.name

        try:
            async with httpx.AsyncClient(
                cert=(cert_path, key_path),
                verify=True,
                timeout=20,
            ) as http:
                resp = await http.post(
                    _ESOCIAL_URL,
                    content=soap_body.encode("utf-8"),
                    headers={
                        "Content-Type": "text/xml; charset=utf-8",
                        "SOAPAction": "ConsultarLoteEventos",
                    },
                )

            folhas = _parse_esocial_soap(resp.text, ano)
            return {
                "status": "ok",
                "status_http": resp.status_code,
                "folhas": folhas,
                "aviso": (
                    "Dados extraídos com sucesso." if folhas
                    else "Conexão com eSocial estabelecida. O webservice pode exigir assinatura XML (xmldsig) "
                         "para retornar eventos — recurso em desenvolvimento. "
                         "Lance os valores manualmente pelo portal do eSocial."
                ),
            }
        finally:
            os.unlink(cert_path)
            os.unlink(key_path)

    except Exception as e:
        msg = str(e)
        if "ConnectError" in msg or "timeout" in msg.lower():
            raise HTTPException(502, "Não foi possível conectar ao eSocial. Verifique a conexão.")
        raise HTTPException(502, f"Erro ao acessar eSocial: {msg}")


# ─── Parsers de resposta ───────────────────────────────────────────────────────

def _parse_pgdas_html(html: str, ano: int) -> list:
    """Tenta extrair competência + receita bruta da página HTML do PGDAS-D."""
    import re
    resultados = []
    # Padrão típico: "01/2025" seguido de valor monetário "R$ 12.345,67"
    pattern = re.compile(
        r'(\d{2}/\d{4})[^<]*?R\$\s*([\d.,]+)',
        re.DOTALL,
    )
    for m in pattern.finditer(html):
        comp = m.group(1)  # MM/YYYY
        val_str = m.group(2).replace(".", "").replace(",", ".")
        try:
            comp_ano = int(comp.split("/")[1])
            if comp_ano == ano:
                resultados.append({
                    "competencia": f"{comp.split('/')[1]}-{comp.split('/')[0]}",
                    "valor_receita": float(val_str),
                    "origem": "pgdas_d",
                })
        except Exception:
            pass
    return resultados


def _parse_esocial_soap(xml: str, ano: int) -> list:
    """Tenta extrair valores de remuneração da resposta SOAP do eSocial."""
    import re
    resultados = []
    # Busca tags de remuneração no XML de retorno
    comp_pat = re.compile(r'<perApur>(\d{4}-\d{2})</perApur>')
    vr_pat   = re.compile(r'<vrTotal>([\d.]+)</vrTotal>')
    comps = comp_pat.findall(xml)
    vrs   = vr_pat.findall(xml)
    for comp, vr in zip(comps, vrs):
        if str(ano) in comp:
            resultados.append({
                "competencia": comp,
                "valor_total_folha": float(vr),
                "origem": "esocial",
            })
    return resultados
