"""
Serviço de emissão de NF-e (Nota Fiscal Eletrônica — modelo 55, produtos).

A NF-e requer:
  - Assinatura XML com certificado digital A1 (RSA-SHA1, xmlsec)
  - Comunicação SOAP com WebService SEFAZ de cada estado
  - Geração do DANFE (PDF)

Este módulo implementa:
  - Modo mock: gera NF-e simulada para desenvolvimento
  - Modo real: estrutura pronta para integrar via nfelib (PyPI: nfelib)

Para produção com nfelib:
  pip install nfelib lxml signxml
  Configure cliente.nfe_certificado_path + cliente.nfe_certificado_senha
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from ..models import Cliente, Nota
from ..schemas import EmitirNFeRequest
from ..config import settings


async def emitir_nfe(
    req: EmitirNFeRequest,
    cliente: Cliente,
    nota: Nota,
) -> dict[str, Any]:
    if settings.MOCK_MODE:
        return await _emitir_nfe_mock(req, cliente, nota)
    return await _emitir_nfe_real(req, cliente, nota)


async def cancelar_nfe(nota: Nota, motivo: str, cliente: Cliente) -> dict[str, Any]:
    if settings.MOCK_MODE or nota.provider == "mock":
        return {"status": "cancelada", "provider": "mock"}
    return await _cancelar_nfe_real(nota, motivo, cliente)


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------

async def _emitir_nfe_mock(
    req: EmitirNFeRequest, cliente: Cliente, nota: Nota
) -> dict[str, Any]:
    numero = str(nota.nfe_ultimo_numero if hasattr(nota, "nfe_ultimo_numero") else 1).zfill(9)
    chave = _gerar_chave_acesso_mock(cliente.uf or "AM", numero, cliente.nfe_serie or "1")
    provider_id = chave

    total_produtos = sum(i.quantidade * i.valor_unitario for i in req.itens)
    total_icms = sum(
        i.quantidade * i.valor_unitario * (i.aliquota_icms or 0) / 100 for i in req.itens
    )

    xml = _gerar_xml_nfe_mock(req, cliente, nota, numero, chave, total_produtos)

    return {
        "status": "emitida",
        "provider": "mock",
        "provider_id": provider_id,
        "provider_status": "100",
        "numero": numero,
        "serie": cliente.nfe_serie or "1",
        "chave_acesso": chave,
        "data_emissao": datetime.now(timezone.utc).isoformat(),
        "valor_servico": total_produtos,
        "valor_iss": 0,
        "valor_liquido": total_produtos,
        "aliquota_iss": 0,
        "xml_content": xml,
        "link_pdf": f"/api/notas/{nota.id}/pdf",
        "link_xml": f"/api/notas/{nota.id}/xml",
    }


def _gerar_chave_acesso_mock(uf_codigo: str, numero: str, serie: str) -> str:
    cuf = {
        "AC": "12", "AL": "27", "AP": "16", "AM": "13", "BA": "29",
        "CE": "23", "DF": "53", "ES": "32", "GO": "52", "MA": "21",
        "MT": "51", "MS": "50", "MG": "31", "PA": "15", "PB": "25",
        "PR": "41", "PE": "26", "PI": "22", "RJ": "33", "RN": "24",
        "RS": "43", "RO": "11", "RR": "14", "SC": "42", "SP": "35",
        "SE": "28", "TO": "17",
    }.get(uf_codigo, "13")
    rand = uuid.uuid4().hex[:20]
    return f"{cuf}{datetime.now().strftime('%y%m')}{rand}{serie.zfill(3)}{numero.zfill(9)}1"


def _gerar_xml_nfe_mock(
    req: EmitirNFeRequest, cliente: Cliente, nota: Nota,
    numero: str, chave: str, total: float,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    itens_xml = ""
    for idx, item in enumerate(req.itens, 1):
        val = round(item.quantidade * item.valor_unitario, 2)
        itens_xml += f"""
    <det nItem="{idx}">
      <prod>
        <cProd>{item.codigo_produto or f"PROD{idx:03d}"}</cProd>
        <cEAN>SEM GTIN</cEAN>
        <xProd>{item.descricao}</xProd>
        <NCM>{item.ncm}</NCM>
        <CFOP>{item.cfop}</CFOP>
        <uCom>{item.unidade}</uCom>
        <qCom>{item.quantidade}</qCom>
        <vUnCom>{item.valor_unitario:.2f}</vUnCom>
        <vProd>{val:.2f}</vProd>
      </prod>
    </det>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
  <NFe>
    <infNFe Id="NFe{chave}" versao="4.00">
      <ide>
        <cUF>13</cUF>
        <natOp>{req.natureza_operacao}</natOp>
        <mod>55</mod>
        <serie>{cliente.nfe_serie or "1"}</serie>
        <nNF>{numero}</nNF>
        <dhEmi>{now}</dhEmi>
        <tpNF>1</tpNF>
        <tpAmb>2</tpAmb>
      </ide>
      <emit>
        <CNPJ>{cliente.cnpj}</CNPJ>
        <xNome>{cliente.razao_social}</xNome>
      </emit>
      <dest>
        <CNPJ>{req.tomador.cpf_cnpj}</CNPJ>
        <xNome>{req.tomador.razao_social}</xNome>
      </dest>
      {itens_xml}
      <total>
        <ICMSTot>
          <vNF>{total:.2f}</vNF>
        </ICMSTot>
      </total>
    </infNFe>
  </NFe>
  <protNFe versao="4.00">
    <infProt>
      <chNFe>{chave}</chNFe>
      <dhRecbto>{now}</dhRecbto>
      <nProt>MOCK{uuid.uuid4().hex[:15].upper()}</nProt>
      <digVal>MOCK</digVal>
      <cStat>100</cStat>
      <xMotivo>Autorizado o uso da NF-e (SIMULAÇÃO)</xMotivo>
    </infProt>
  </protNFe>
</nfeProc>"""


# ---------------------------------------------------------------------------
# Real (nfelib) — Esqueleto pronto para produção
# ---------------------------------------------------------------------------

async def _emitir_nfe_real(
    req: EmitirNFeRequest, cliente: Cliente, nota: Nota
) -> dict[str, Any]:
    """
    Integração real com SEFAZ via nfelib.

    Instale: pip install nfelib lxml signxml requests

    Passos:
    1. Gerar XML da NF-e (nfelib.nf_e.v4_0.leiaute_nfe_v4_00 classes)
    2. Assinar com certificado A1 (signxml.XMLSigner)
    3. Enviar para WebService SEFAZ do estado (nfelib.nf_e.ws)
    4. Processar resposta (cStat 100 = autorizada)
    5. Salvar XML autorizado + gerar DANFE (reportlab)

    Esta função levanta NotImplementedError para deixar explícito
    que o integrando deve completar conforme suas chaves.
    """
    raise NotImplementedError(
        "Emissão real de NF-e requer certificado A1 e integração com SEFAZ estadual. "
        "Defina MOCK_MODE=False e implemente esta função com nfelib. "
        "Consulte: https://pypi.org/project/nfelib/"
    )


async def _cancelar_nfe_real(nota: Nota, motivo: str, cliente: Cliente) -> dict[str, Any]:
    raise NotImplementedError(
        "Cancelamento real de NF-e requer certificado A1 e integração com SEFAZ estadual."
    )
