"""
Serviço de emissão de NFS-e com roteamento híbrido:

  1. mock    → Gera nota simulada (dev / demo)
  2. nfeio   → NFe.io REST API (agregadora, cobre 5.500+ municípios)
  3. nacional → NFS-e Nacional SEFAZ (padrão DPS — municípios aderentes)

Cada cliente configura seu provider em Cliente.nfse_provider.
"""

import httpx
import uuid
import json
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..models import Cliente, Nota
from ..schemas import EmitirNFSeRequest


# ---------------------------------------------------------------------------
# Roteador principal
# ---------------------------------------------------------------------------

async def emitir_nfse(
    req: EmitirNFSeRequest,
    cliente: Cliente,
    nota: Nota,
) -> dict[str, Any]:
    provider = cliente.nfse_provider.value if cliente.nfse_provider else "mock"

    if provider == "nfeio":
        return await _emitir_nfeio(req, cliente, nota)
    elif provider == "nacional":
        return await _emitir_nacional(req, cliente, nota)
    else:
        return await _emitir_mock(req, cliente, nota)


async def cancelar_nfse(nota: Nota, motivo: str, cliente: Cliente) -> dict[str, Any]:
    provider = nota.provider or "mock"

    if provider == "nfeio":
        return await _cancelar_nfeio(nota, motivo, cliente)
    elif provider == "nacional":
        return await _cancelar_nacional(nota, motivo, cliente)
    else:
        return {"status": "cancelada", "provider": "mock"}


# ---------------------------------------------------------------------------
# Provedor 1: Mock (desenvolvimento / demo)
# ---------------------------------------------------------------------------

async def _emitir_mock(
    req: EmitirNFSeRequest, cliente: Cliente, nota: Nota
) -> dict[str, Any]:
    numero = str(nota.numero_rps or 1).zfill(8)
    provider_id = f"MOCK-{uuid.uuid4().hex[:12].upper()}"
    aliquota = req.aliquota_iss or cliente.nfse_aliquota_iss or 5.0
    valor_iss = round(req.valor_servico * aliquota / 100, 2)
    valor_liquido = round(
        req.valor_servico - valor_iss - req.valor_pis - req.valor_cofins
        - req.valor_inss - req.valor_ir - req.valor_csll,
        2,
    )

    xml_simulado = _gerar_xml_mock(req, cliente, nota, numero, provider_id)

    return {
        "status": "emitida",
        "provider": "mock",
        "provider_id": provider_id,
        "numero": numero,
        "chave_acesso": provider_id,
        "data_emissao": datetime.now(timezone.utc).isoformat(),
        "valor_iss": valor_iss,
        "valor_liquido": valor_liquido,
        "aliquota_iss": aliquota,
        "xml_content": xml_simulado,
        "link_pdf": f"/api/notas/{nota.id}/pdf",
        "link_xml": f"/api/notas/{nota.id}/xml",
    }


def _gerar_xml_mock(
    req: EmitirNFSeRequest, cliente: Cliente, nota: Nota,
    numero: str, provider_id: str,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<CompNfse xmlns="http://www.abrasf.org.br/nfse.xsd">
  <Nfse>
    <InfNfse>
      <Numero>{numero}</Numero>
      <CodigoVerificacao>{provider_id}</CodigoVerificacao>
      <DataEmissao>{now}</DataEmissao>
      <Competencia>{req.data_competencia}-01</Competencia>
      <PrestadorServico>
        <IdentificacaoPrestador>
          <Cnpj>{cliente.cnpj}</Cnpj>
        </IdentificacaoPrestador>
        <RazaoSocial>{cliente.razao_social}</RazaoSocial>
      </PrestadorServico>
      <TomadorServico>
        <IdentificacaoTomador>
          <CpfCnpj><Cnpj>{req.tomador.cpf_cnpj}</Cnpj></CpfCnpj>
        </IdentificacaoTomador>
        <RazaoSocial>{req.tomador.razao_social}</RazaoSocial>
      </TomadorServico>
      <Servico>
        <CodigoCnae>{req.codigo_servico_lc116}</CodigoCnae>
        <Discriminacao>{req.discriminacao}</Discriminacao>
        <ValorServicos>{req.valor_servico:.2f}</ValorServicos>
        <IssRetido>{'1' if req.iss_retido else '2'}</IssRetido>
        <Aliquota>{req.aliquota_iss or cliente.nfse_aliquota_iss or 5.0:.4f}</Aliquota>
      </Servico>
    </InfNfse>
  </Nfse>
</CompNfse>"""


# ---------------------------------------------------------------------------
# Provedor 2: NFe.io (agregadora comercial)
# ---------------------------------------------------------------------------

async def _emitir_nfeio(
    req: EmitirNFSeRequest, cliente: Cliente, nota: Nota
) -> dict[str, Any]:
    api_key = cliente.nfse_api_key or settings.NFEIO_API_KEY
    company_id = cliente.nfse_company_id

    if not api_key or not company_id:
        raise ValueError(
            "Cliente sem API Key ou Company ID do NFe.io configurados. "
            "Acesse as configurações do cliente e preencha os dados do NFe.io."
        )

    aliquota = req.aliquota_iss or cliente.nfse_aliquota_iss or 5.0

    payload = {
        "cityServiceCode": req.codigo_servico_municipal or req.codigo_servico_lc116,
        "federalServiceCode": req.codigo_servico_lc116,
        "description": req.discriminacao,
        "servicesAmount": req.valor_servico,
        "deductionsAmount": req.valor_deducoes,
        "issRate": aliquota / 100,
        "issWithheld": req.iss_retido,
        "pisAmount": req.valor_pis,
        "cofinsAmount": req.valor_cofins,
        "inssAmount": req.valor_inss,
        "irAmount": req.valor_ir,
        "csllAmount": req.valor_csll,
        "serviceLocation": {
            "city": {
                "code": cliente.nfse_municipio_codigo or cliente.codigo_ibge or "",
                "name": cliente.municipio or "",
            },
            "state": cliente.uf or "",
        },
        "borrower": {
            "federalTaxNumber": "".join(c for c in (req.tomador.cpf_cnpj or "") if c.isdigit()),
            "name": req.tomador.razao_social,
            "email": req.tomador.email or "",
            "address": {
                "country": "BRA",
                "postalCode": "".join(c for c in (req.tomador.cep or "") if c.isdigit()),
                "street": req.tomador.logradouro or "",
                "number": req.tomador.numero or "S/N",
                "additionalInformation": req.tomador.complemento or "",
                "district": req.tomador.bairro or "",
                "city": {
                    "code": req.tomador.codigo_ibge or "",
                    "name": req.tomador.municipio or "",
                },
                "state": req.tomador.uf or "",
            },
        },
    }

    async with httpx.AsyncClient(timeout=30) as client_http:
        resp = await client_http.post(
            f"{settings.NFEIO_API_URL}/companies/{company_id}/serviceinvoices",
            json=payload,
            headers={"Authorization": f"Basic {api_key}"},
        )

    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"NFe.io retornou {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    return {
        "status": "emitida" if data.get("flowStatus") == "Issued" else "pendente",
        "provider": "nfeio",
        "provider_id": data.get("id", ""),
        "provider_status": data.get("flowStatus", ""),
        "numero": data.get("number", ""),
        "chave_acesso": data.get("checkCode", ""),
        "data_emissao": data.get("issuedOn", datetime.now(timezone.utc).isoformat()),
        "valor_iss": data.get("issAmount", 0),
        "valor_liquido": data.get("netAmount", req.valor_servico),
        "aliquota_iss": aliquota,
        "link_pdf": data.get("pdfFileUrl", ""),
        "link_xml": data.get("xmlFileUrl", ""),
    }


async def _cancelar_nfeio(nota: Nota, motivo: str, cliente: Cliente) -> dict[str, Any]:
    api_key = cliente.nfse_api_key or settings.NFEIO_API_KEY
    company_id = cliente.nfse_company_id

    async with httpx.AsyncClient(timeout=30) as client_http:
        resp = await client_http.delete(
            f"{settings.NFEIO_API_URL}/companies/{company_id}/serviceinvoices/{nota.provider_id}",
            headers={"Authorization": f"Basic {api_key}"},
        )

    if resp.status_code not in (200, 202, 204):
        raise RuntimeError(f"NFe.io cancelamento retornou {resp.status_code}: {resp.text[:500]}")

    return {"status": "cancelada", "provider": "nfeio"}


# ---------------------------------------------------------------------------
# Provedor 3: NFS-e Nacional (SEFAZ) — DPS
# ---------------------------------------------------------------------------

async def _emitir_nacional(
    req: EmitirNFSeRequest, cliente: Cliente, nota: Nota
) -> dict[str, Any]:
    """
    Emite NFS-e via Sistema Nacional NFS-e (SEFAZ).

    Requer:
    - Certificado digital A1 do prestador (cliente.nfe_certificado_path)
    - Token OAuth2 do Gov.br (não implementado aqui — usar flow separado)

    Por enquanto, faz a chamada sem assinatura de certificado (homologação
    com token via Gov.br). Para produção, adicionar signxml para assinar o DPS.
    """
    base_url = (
        settings.NFSE_NACIONAL_URL_PRODUCAO
        if settings.NFSE_AMBIENTE == "1"
        else settings.NFSE_NACIONAL_URL_HOMOLOGACAO
    )

    aliquota = req.aliquota_iss or cliente.nfse_aliquota_iss or 5.0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-03:00")

    dps_payload = {
        "infDPS": {
            "tpAmb": int(settings.NFSE_AMBIENTE),
            "dhEmi": now,
            "verAplic": "1.00",
            "dCompet": req.data_competencia,
            "prest": {
                "CNPJ": cliente.cnpj,
                "xNome": cliente.razao_social,
                "end": {
                    "xLgr": cliente.logradouro or "",
                    "nro": cliente.numero or "S/N",
                    "xBairro": cliente.bairro or "",
                    "cMun": cliente.codigo_ibge or "",
                    "xMun": cliente.municipio or "",
                    "CEP": "".join(c for c in (cliente.cep or "") if c.isdigit()),
                    "cPais": "1058",
                    "UF": cliente.uf or "",
                },
                "fone": "".join(c for c in (cliente.telefone or "") if c.isdigit()),
                "email": cliente.email or "",
            },
            "toma": {
                "CNPJ" if len("".join(c for c in req.tomador.cpf_cnpj if c.isdigit())) == 14 else "CPF":
                    "".join(c for c in req.tomador.cpf_cnpj if c.isdigit()),
                "xNome": req.tomador.razao_social,
                "end": {
                    "xLgr": req.tomador.logradouro or "",
                    "nro": req.tomador.numero or "S/N",
                    "xBairro": req.tomador.bairro or "",
                    "cMun": req.tomador.codigo_ibge or "",
                    "xMun": req.tomador.municipio or "",
                    "CEP": "".join(c for c in (req.tomador.cep or "") if c.isdigit()),
                    "cPais": "1058",
                    "UF": req.tomador.uf or "",
                },
                "email": req.tomador.email or "",
            },
            "serv": {
                "locPrest": {
                    "cMun": cliente.codigo_ibge or "",
                    "cPais": "1058",
                },
                "cServ": {
                    "cTribNac": req.codigo_servico_lc116,
                    "cTribMun": req.codigo_servico_municipal or "",
                    "xDescServ": req.discriminacao[:2000],
                },
            },
            "valores": {
                "vServPrest": req.valor_servico,
                "vDesc": req.valor_deducoes,
                "vDed": 0,
                "tributos": {
                    "tribMun": {
                        "tribISSQN": {
                            "cLocIncid": cliente.codigo_ibge or "",
                            "indISSRet": 1 if req.iss_retido else 2,
                            "vBC": req.valor_servico - req.valor_deducoes,
                            "pAliq": aliquota,
                            "vISSQN": round(
                                (req.valor_servico - req.valor_deducoes) * aliquota / 100, 2
                            ),
                        }
                    }
                },
            },
            "infoCompl": {"xInfComp": req.discriminacao},
        }
    }

    # Nota: em produção, o DPS precisa ser assinado com certificado A1 antes
    # de ser enviado. Use `signxml` + `lxml` para isso.
    token = cliente.nfse_api_key or ""  # token Gov.br armazenado na api_key por ora

    async with httpx.AsyncClient(timeout=30) as client_http:
        resp = await client_http.post(
            f"{base_url}/nfse",
            json=dps_payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"NFS-e Nacional retornou {resp.status_code}: {resp.text[:500]}"
        )

    data = resp.json()
    numero = data.get("nNFSe") or data.get("numero") or str(nota.numero_rps or 1)

    return {
        "status": "emitida",
        "provider": "nacional",
        "provider_id": data.get("chNFSe") or data.get("id") or numero,
        "provider_status": data.get("cStat", "100"),
        "numero": numero,
        "chave_acesso": data.get("chNFSe", ""),
        "data_emissao": data.get("dhEmi", now),
        "valor_iss": round(
            (req.valor_servico - req.valor_deducoes) * aliquota / 100, 2
        ),
        "valor_liquido": round(req.valor_servico - req.valor_pis - req.valor_cofins, 2),
        "aliquota_iss": aliquota,
        "xml_content": json.dumps(dps_payload),
        "link_pdf": data.get("urlPdf", f"/api/notas/{nota.id}/pdf"),
        "link_xml": data.get("urlXml", f"/api/notas/{nota.id}/xml"),
    }


async def _cancelar_nacional(nota: Nota, motivo: str, cliente: Cliente) -> dict[str, Any]:
    base_url = (
        settings.NFSE_NACIONAL_URL_PRODUCAO
        if settings.NFSE_AMBIENTE == "1"
        else settings.NFSE_NACIONAL_URL_HOMOLOGACAO
    )
    token = cliente.nfse_api_key or ""

    async with httpx.AsyncClient(timeout=30) as client_http:
        resp = await client_http.delete(
            f"{base_url}/nfse/{nota.provider_id}",
            json={"xJust": motivo},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code not in (200, 202, 204):
        raise RuntimeError(
            f"NFS-e Nacional cancelamento retornou {resp.status_code}: {resp.text[:500]}"
        )

    return {"status": "cancelada", "provider": "nacional"}
