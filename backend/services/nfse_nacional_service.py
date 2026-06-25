"""
NFS-e Nacional (SEFAZ) — consulta NFS-e recebidas como tomador.

API REST: https://www.nfse.gov.br/nfse/api/v1
Auth: mTLS com certificado A1 (mesmo .pfx da NF-e)
Paginação: NSU incremental (igual ao DF-e da NF-e)

Documentação: Manual de Integração NFS-e Nacional NT 001/2023+
"""
import os
import tempfile
from datetime import datetime
from typing import Optional

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates

NFSE_NAC_PROD = "https://www.nfse.gov.br/nfse/api/v1"
NFSE_NAC_HOM  = "https://homolog.nfse.gov.br/nfse/api/v1"


def _load_pfx(cert_path: str, senha: str):
    with open(cert_path, "rb") as f:
        data = f.read()
    key, cert, _ = load_key_and_certificates(data, senha.encode())
    return key, cert


def _pem_files(key, cert):
    cp = cert.public_bytes(serialization.Encoding.PEM)
    kp = key.private_bytes(serialization.Encoding.PEM,
                           serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
    fc = tempfile.NamedTemporaryFile(suffix="_cert.pem", delete=False)
    fk = tempfile.NamedTemporaryFile(suffix="_key.pem",  delete=False)
    fc.write(cp); fc.close()
    fk.write(kp); fk.close()
    return fc.name, fk.name


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19])
    except Exception:
        return None


def _parse_nfse(item: dict) -> dict:
    """Normaliza um item da resposta JSON da NFS-e Nacional para o formato padrão."""
    prest = item.get("prestador") or item.get("emit") or {}
    val   = item.get("valores") or item.get("servico", {}).get("valores") or {}
    serv  = item.get("servico") or {}

    cnpj_prest = (prest.get("cpfCnpj") or prest.get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
    nome_prest = prest.get("razaoSocial") or prest.get("nome") or ""
    uf_prest   = (prest.get("endereco") or {}).get("uf") or prest.get("uf") or ""
    mun_prest  = (prest.get("endereco") or {}).get("xMun") or prest.get("municipio") or ""
    valor      = float(val.get("vNfse") or val.get("vLiquidoNfse") or val.get("vServico") or 0)
    chave      = item.get("chaveNfse") or item.get("id") or item.get("numero") or ""
    numero     = str(item.get("numero") or "")
    serie      = str(item.get("serie") or "")
    dt_emissao = _parse_iso(item.get("dataEmissao") or item.get("dhEmissao"))
    discriminacao = serv.get("discriminacao") or item.get("descricao") or ""

    return {
        "chave": chave,
        "tipo": "nfse",
        "numero": numero,
        "serie": serie,
        "data_emissao": dt_emissao,
        "emitente_cnpj": cnpj_prest,
        "emitente_razao_social": nome_prest,
        "emitente_uf": uf_prest,
        "emitente_municipio": mun_prest,
        "valor_total": valor,
        "natureza_operacao": discriminacao[:100] if discriminacao else None,
        "status_manifestacao": "ciencia_operacao",
        "xml_content": None,
    }


async def consultar_nfse_nacional(
    cnpj: str,
    cert_path: str,
    cert_senha: str,
    ultimo_nsu: str = "0",
    producao: bool = True,
) -> dict:
    """
    Consulta NFS-e recebidas como tomador via API NFS-e Nacional SEFAZ.

    Endpoint: GET /dps/tomador/{cnpj}/consulta?ultNSU={nsu}
    Retorna: { ultimo_nsu, nfse: [...], cstat, xmotivo }
    """
    base = NFSE_NAC_PROD if producao else NFSE_NAC_HOM
    cnpj_dig = "".join(c for c in cnpj if c.isdigit())

    key, cert = _load_pfx(cert_path, cert_senha)
    cert_tmp, key_tmp = _pem_files(key, cert)

    try:
        async with httpx.AsyncClient(cert=(cert_tmp, key_tmp), verify=True, timeout=60.0) as http:
            resp = await http.get(
                f"{base}/dps/tomador/{cnpj_dig}/consulta",
                params={"ultNSU": str(ultimo_nsu).zfill(15)},
                headers={"Accept": "application/json"},
            )
    finally:
        os.unlink(cert_tmp)
        os.unlink(key_tmp)

    if resp.status_code == 404:
        return {"ultimo_nsu": ultimo_nsu, "nfse": [], "cstat": "138", "xmotivo": "Sem documentos"}

    if resp.status_code not in (200, 206):
        raise RuntimeError(f"NFS-e Nacional HTTP {resp.status_code}: {resp.text[:400]}")

    data = resp.json()

    # Normaliza o formato da resposta (pode variar entre versões da API)
    if isinstance(data, list):
        items   = data
        ult_nsu = ultimo_nsu
        cstat   = "137"
        xmotivo = "Documento(s) localizado(s)"
    else:
        items   = data.get("nfse") or data.get("documentos") or data.get("data") or []
        ult_nsu = str(data.get("ultNSU") or data.get("ultimoNSU") or ultimo_nsu)
        cstat   = str(data.get("cStat") or ("137" if items else "138"))
        xmotivo = data.get("xMotivo") or ""

    return {
        "ultimo_nsu": ult_nsu,
        "nfse": [_parse_nfse(i) for i in items],
        "cstat": cstat,
        "xmotivo": xmotivo,
    }
