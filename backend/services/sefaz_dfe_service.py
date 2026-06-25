"""
SEFAZ NFeDistribuicaoDFe — consulta NF-e recebidas pelo destinatário.

Usa o webservice nacional (AN) via SOAP 1.2 com:
- mTLS (certificado A1 como client certificate no HTTPS)
- Assinatura XML enveloped (RSA-SHA256) no elemento distDFeInt

Documentação: NT 2013/005 e Manual de Orientação ao Contribuinte v7.00.
"""
import base64
import gzip
import hashlib
import os
import tempfile
from datetime import datetime
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates
from lxml import etree

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

DFE_URL_PROD = "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx"
DFE_URL_HOM  = "https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx"
SOAP_ACTION  = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe/nfeDistDFeInteresse"

NS_NFE  = "http://www.portalfiscal.inf.br/nfe"
NS_SIG  = "http://www.w3.org/2000/09/xmldsig#"
NS_WSDL = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe"

UF_CODES: dict[str, str] = {
    "AC": "12", "AL": "27", "AP": "16", "AM": "13", "BA": "29",
    "CE": "23", "DF": "53", "ES": "32", "GO": "52", "MA": "21",
    "MT": "51", "MS": "50", "MG": "31", "PA": "15", "PB": "25",
    "PR": "41", "PE": "26", "PI": "22", "RJ": "33", "RN": "24",
    "RS": "43", "RO": "11", "RR": "14", "SC": "42", "SP": "35",
    "SE": "28", "TO": "17",
}


# ---------------------------------------------------------------------------
# Certificado
# ---------------------------------------------------------------------------

def _load_pfx(cert_path: str, senha: str):
    with open(cert_path, "rb") as f:
        data = f.read()
    key, cert, _ = load_key_and_certificates(data, senha.encode("utf-8"))
    return key, cert


def _cert_pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


# ---------------------------------------------------------------------------
# Assinatura XML (enveloped, RSA-SHA256, C14N sem exclusive)
# ---------------------------------------------------------------------------

def _sign_dist_dfe_int(xml_bytes: bytes, key, cert) -> str:
    """
    Assina o elemento distDFeInt com assinatura enveloped.
    Retorna string XML com o elemento <Signature> inserido como filho.
    """
    root = etree.fromstring(xml_bytes)
    root.set("Id", "distDFeInt")

    # 1. C14N do elemento (sem Signature ainda) → digest SHA-256
    c14n_ref = etree.tostring(root, method="c14n", with_comments=False)
    digest_b64 = base64.b64encode(hashlib.sha256(c14n_ref).digest()).decode()

    # 2. Monta SignedInfo
    si = etree.Element(f"{{{NS_SIG}}}SignedInfo")
    cm = etree.SubElement(si, f"{{{NS_SIG}}}CanonicalizationMethod")
    cm.set("Algorithm", "http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    sm = etree.SubElement(si, f"{{{NS_SIG}}}SignatureMethod")
    sm.set("Algorithm", "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256")
    ref_el = etree.SubElement(si, f"{{{NS_SIG}}}Reference")
    ref_el.set("URI", "#distDFeInt")
    trs = etree.SubElement(ref_el, f"{{{NS_SIG}}}Transforms")
    t1 = etree.SubElement(trs, f"{{{NS_SIG}}}Transform")
    t1.set("Algorithm", "http://www.w3.org/2000/09/xmldsig#enveloped-signature")
    t2 = etree.SubElement(trs, f"{{{NS_SIG}}}Transform")
    t2.set("Algorithm", "http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    dm = etree.SubElement(ref_el, f"{{{NS_SIG}}}DigestMethod")
    dm.set("Algorithm", "http://www.w3.org/2001/04/xmlenc#sha256")
    dv = etree.SubElement(ref_el, f"{{{NS_SIG}}}DigestValue")
    dv.text = digest_b64

    # 3. C14N do SignedInfo → assina com RSA-SHA256
    c14n_si = etree.tostring(si, method="c14n", with_comments=False)
    sig_bytes = key.sign(c14n_si, asym_padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(sig_bytes).decode()

    # 4. KeyInfo: certificado em DER/base64
    cert_der_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()

    # 5. Monta e insere elemento Signature
    sig_el = etree.SubElement(root, f"{{{NS_SIG}}}Signature")
    sig_el.append(si)
    sv = etree.SubElement(sig_el, f"{{{NS_SIG}}}SignatureValue")
    sv.text = sig_b64
    ki = etree.SubElement(sig_el, f"{{{NS_SIG}}}KeyInfo")
    x509d = etree.SubElement(ki, f"{{{NS_SIG}}}X509Data")
    x509c = etree.SubElement(x509d, f"{{{NS_SIG}}}X509Certificate")
    x509c.text = cert_der_b64

    return etree.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# SOAP envelope
# ---------------------------------------------------------------------------

def _build_soap(signed_xml_str: str, uf_code: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        ' xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">'
        "<soap12:Header>"
        f'<nfeCabecMsg xmlns="{NS_WSDL}">'
        f"<cUF>{uf_code}</cUF>"
        "<versaoDados>1.01</versaoDados>"
        "</nfeCabecMsg>"
        "</soap12:Header>"
        "<soap12:Body>"
        f'<nfeDistDFeInteresse xmlns="{NS_WSDL}">'
        f"<nfeDadosMsg>{signed_xml_str}</nfeDadosMsg>"
        "</nfeDistDFeInteresse>"
        "</soap12:Body>"
        "</soap12:Envelope>"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Parser de documentos retornados
# ---------------------------------------------------------------------------

def _decompress(doc_zip_b64: str) -> bytes:
    return gzip.decompress(base64.b64decode(doc_zip_b64))


def _parse_text(root, *xpaths, ns) -> Optional[str]:
    for path in xpaths:
        el = root.find(path, ns)
        if el is not None and el.text:
            return el.text.strip()
    return None


def _extract_fields(xml_bytes: bytes, schema: str) -> dict:
    """Extrai campos relevantes de procNFe, resNFe ou procEventoNFe."""
    try:
        root = etree.fromstring(xml_bytes)
        ns = {"nfe": NS_NFE}

        if "resNFe" in schema:
            # Resumo de NF-e: só metadados
            chave  = _parse_text(root, "nfe:chNFe", ns=ns)
            num    = _parse_text(root, "nfe:nNF", "nfe:nDoc", ns=ns)
            valor  = _parse_text(root, "nfe:dest/nfe:vNF", "nfe:vNF", ns=ns)
            emcnpj = _parse_text(root, "nfe:CNPJ", ns=ns)
            emuf   = _parse_text(root, "nfe:cUFSaida", ns=ns)
            dt_str = _parse_text(root, "nfe:dEmi", "nfe:dhRecbto", ns=ns)
            mod    = _parse_text(root, "nfe:mod", ns=ns)
            tipo   = "nfce" if mod == "65" else "nfe"
            data_emissao = _safe_dt(dt_str)
            return {
                "chave": chave, "tipo": tipo, "numero": num, "serie": None,
                "data_emissao": data_emissao,
                "emitente_cnpj": emcnpj, "emitente_razao_social": None,
                "emitente_uf": emuf, "emitente_municipio": None,
                "valor_total": float(valor) if valor else None,
                "natureza_operacao": None,
                "status_manifestacao": "pendente",
                "xml_content": None,
            }

        if "procNFe" in schema or "NFe" in schema:
            # XML completo da NF-e
            info = root.find(".//nfe:infNFe", ns)
            if info is None:
                return {}
            chave  = (info.get("Id") or "").replace("NFe", "") or None
            num    = _parse_text(info, "nfe:ide/nfe:nNF", ns=ns)
            serie  = _parse_text(info, "nfe:ide/nfe:serie", ns=ns)
            mod    = _parse_text(info, "nfe:ide/nfe:mod", ns=ns)
            nat_op = _parse_text(info, "nfe:ide/nfe:natOp", ns=ns)
            dt_str = _parse_text(info, "nfe:ide/nfe:dhEmi", "nfe:ide/nfe:dEmi", ns=ns)
            emcnpj = _parse_text(info, "nfe:emit/nfe:CNPJ", "nfe:emit/nfe:CPF", ns=ns)
            emnome = _parse_text(info, "nfe:emit/nfe:xNome", ns=ns)
            emuf   = _parse_text(info, "nfe:emit/nfe:enderEmit/nfe:UF", ns=ns)
            emmun  = _parse_text(info, "nfe:emit/nfe:enderEmit/nfe:xMun", ns=ns)
            valor  = _parse_text(info, "nfe:total/nfe:ICMSTot/nfe:vNF", ns=ns)
            tipo   = "nfce" if mod == "65" else "nfe"
            return {
                "chave": chave, "tipo": tipo, "numero": num, "serie": serie,
                "data_emissao": _safe_dt(dt_str),
                "emitente_cnpj": emcnpj, "emitente_razao_social": emnome,
                "emitente_uf": emuf, "emitente_municipio": emmun,
                "valor_total": float(valor) if valor else None,
                "natureza_operacao": nat_op,
                "status_manifestacao": "ciencia_operacao",
                "xml_content": xml_bytes.decode("utf-8", errors="replace"),
            }

        if "procEvento" in schema or "evento" in schema.lower():
            # Evento (cancelamento, carta de correção, etc.) — registra separado
            chave = _parse_text(root, ".//nfe:chNFe", ns=ns)
            tp_ev = _parse_text(root, ".//nfe:tpEvento", ns=ns)
            return {"chave": chave, "tipo_evento": tp_ev, "schema": schema}

    except Exception:
        pass
    return {}


def _safe_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Consulta principal
# ---------------------------------------------------------------------------

async def consultar_dfe(
    cnpj: str,
    uf: str,
    cert_path: str,
    cert_senha: str,
    ultimo_nsu: str = "0",
    producao: bool = True,
) -> dict:
    """
    Consulta o SEFAZ DF-e pelo webservice NFeDistribuicaoDFe.

    Retorna dict com:
        ultimo_nsu  : str  — NSU máximo retornado (salvar para próxima chamada)
        documentos  : list — cada item tem schema, nsu, campos (dict), xml (bytes)
        cstat       : str  — código de status SEFAZ (137=docs encontrados, 138=sem novos)
        xmotivo     : str  — descrição do status
    """
    uf_code  = UF_CODES.get(uf.upper(), "91")
    cnpj_dig = "".join(c for c in cnpj if c.isdigit())
    tp_amb   = "1" if producao else "2"

    # Carrega certificado
    key, cert = _load_pfx(cert_path, cert_senha)

    # Monta e assina distDFeInt
    dist_xml = (
        f'<distDFeInt xmlns="{NS_NFE}" versao="1.01">'
        f"<tpAmb>{tp_amb}</tpAmb>"
        f"<cUFAutor>{uf_code}</cUFAutor>"
        f"<CNPJ>{cnpj_dig}</CNPJ>"
        "<distNSU>"
        f"<ultNSU>{str(ultimo_nsu).zfill(15)}</ultNSU>"
        "</distNSU>"
        "</distDFeInt>"
    )
    signed_xml = _sign_dist_dfe_int(dist_xml.encode(), key, cert)
    soap_body  = _build_soap(signed_xml, uf_code)

    # Exporta cert/key como PEM temporários para mTLS
    cp = _cert_pem(cert)
    kp = _key_pem(key)
    with tempfile.NamedTemporaryFile(suffix="_cert.pem", delete=False) as fc:
        fc.write(cp); cert_tmp = fc.name
    with tempfile.NamedTemporaryFile(suffix="_key.pem", delete=False) as fk:
        fk.write(kp); key_tmp = fk.name

    url = DFE_URL_PROD if producao else DFE_URL_HOM
    try:
        async with httpx.AsyncClient(
            cert=(cert_tmp, key_tmp),
            verify=True,
            timeout=60.0,
        ) as http:
            resp = await http.post(
                url,
                content=soap_body,
                headers={
                    "Content-Type": 'application/soap+xml; charset=utf-8',
                    "SOAPAction": SOAP_ACTION,
                },
            )
    finally:
        os.unlink(cert_tmp)
        os.unlink(key_tmp)

    if resp.status_code != 200:
        raise RuntimeError(f"SEFAZ HTTP {resp.status_code}: {resp.text[:400]}")

    # Parse da resposta SOAP
    ret = etree.fromstring(resp.content).find(f".//{{{NS_NFE}}}retDistDFeInt")
    if ret is None:
        raise RuntimeError(f"Resposta SEFAZ inválida: {resp.text[:400]}")

    def _txt(tag: str) -> str:
        el = ret.find(f"{{{NS_NFE}}}{tag}")
        return el.text if el is not None else ""

    cstat     = _txt("cStat")
    xmotivo   = _txt("xMotivo")
    ult_nsu   = _txt("ultNSU") or ultimo_nsu

    documentos = []
    lote = ret.find(f"{{{NS_NFE}}}loteDistDFeInt")
    if lote is not None:
        for doc_zip in lote.findall(f"{{{NS_NFE}}}docZip"):
            nsu    = doc_zip.get("NSU", "0")
            schema = doc_zip.get("schema", "")
            try:
                xml_bytes = _decompress(doc_zip.text or "")
            except Exception:
                continue
            campos = _extract_fields(xml_bytes, schema)
            documentos.append({"nsu": nsu, "schema": schema, "campos": campos, "xml": xml_bytes})

    return {
        "ultimo_nsu": ult_nsu,
        "documentos": documentos,
        "cstat": cstat,
        "xmotivo": xmotivo,
    }
