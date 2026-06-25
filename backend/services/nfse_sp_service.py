"""
NFS-e São Paulo — WebService próprio da Prefeitura de SP.

Endpoint: https://nfe.prefeitura.sp.gov.br/ws/lotenfe.asmx
Operação: ConsultarNFSeServicoTomado (tomador consulta NFS-e recebidas)
Auth: Assinatura XML com certificado A1 (mesmo .pfx)

Schema: http://www.prefeitura.sp.gov.br/nfe (diferente do ABRASF)
"""
import base64
import hashlib
import os
import tempfile
from datetime import datetime, date
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates
from lxml import etree

SP_WS_URL   = "https://nfe.prefeitura.sp.gov.br/ws/lotenfe.asmx"
SP_SOAP_ACT = "http://www.prefeitura.sp.gov.br/nfe/ws/lotenfe/ConsultarNFSeServicoTomado"
NS_SP       = "http://www.prefeitura.sp.gov.br/nfe"
NS_SIG      = "http://www.w3.org/2000/09/xmldsig#"


# ---------------------------------------------------------------------------
# Certificado (mesmo helper dos outros serviços)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Assinatura XML (enveloped, RSA-SHA256)
# ---------------------------------------------------------------------------

def _sign_element(root, key, cert, ref_id: str) -> None:
    root.set("Id", ref_id)
    c14n_ref = etree.tostring(root, method="c14n", with_comments=False)
    digest_b64 = base64.b64encode(hashlib.sha256(c14n_ref).digest()).decode()

    si = etree.Element(f"{{{NS_SIG}}}SignedInfo")
    cm = etree.SubElement(si, f"{{{NS_SIG}}}CanonicalizationMethod")
    cm.set("Algorithm", "http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    sm = etree.SubElement(si, f"{{{NS_SIG}}}SignatureMethod")
    sm.set("Algorithm", "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256")
    ref_el = etree.SubElement(si, f"{{{NS_SIG}}}Reference")
    ref_el.set("URI", f"#{ref_id}")
    trs = etree.SubElement(ref_el, f"{{{NS_SIG}}}Transforms")
    t1 = etree.SubElement(trs, f"{{{NS_SIG}}}Transform")
    t1.set("Algorithm", "http://www.w3.org/2000/09/xmldsig#enveloped-signature")
    t2 = etree.SubElement(trs, f"{{{NS_SIG}}}Transform")
    t2.set("Algorithm", "http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    dm = etree.SubElement(ref_el, f"{{{NS_SIG}}}DigestMethod")
    dm.set("Algorithm", "http://www.w3.org/2001/04/xmlenc#sha256")
    dv = etree.SubElement(ref_el, f"{{{NS_SIG}}}DigestValue")
    dv.text = digest_b64

    c14n_si  = etree.tostring(si, method="c14n", with_comments=False)
    sig_b64  = base64.b64encode(key.sign(c14n_si, asym_padding.PKCS1v15(), hashes.SHA256())).decode()
    cert_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()

    sig_el = etree.SubElement(root, f"{{{NS_SIG}}}Signature")
    sig_el.append(si)
    sv = etree.SubElement(sig_el, f"{{{NS_SIG}}}SignatureValue")
    sv.text = sig_b64
    ki = etree.SubElement(sig_el, f"{{{NS_SIG}}}KeyInfo")
    x509d = etree.SubElement(ki, f"{{{NS_SIG}}}X509Data")
    x509c = etree.SubElement(x509d, f"{{{NS_SIG}}}X509Certificate")
    x509c.text = cert_b64


# ---------------------------------------------------------------------------
# Monta payload SP
# ---------------------------------------------------------------------------

def _build_consulta_sp(cnpj: str, im_tomador: Optional[str],
                        data_ini: str, data_fim: str, pagina: int = 1) -> str:
    cnpj_dig = "".join(c for c in cnpj if c.isdigit())
    im_tag   = f"<InscricaoMunicipal>{im_tomador}</InscricaoMunicipal>" if im_tomador else ""
    xml_str  = (
        f'<ConsultarNFSeServicoTomadoEnvio xmlns="{NS_SP}" Versao="1">'
        f"<CPFCNPJTomador><CNPJ>{cnpj_dig}</CNPJ></CPFCNPJTomador>"
        f"{im_tag}"
        f"<PeriodoEmissao>"
        f"<DataInicial>{data_ini}</DataInicial>"
        f"<DataFinal>{data_fim}</DataFinal>"
        f"</PeriodoEmissao>"
        f"<Pagina>{pagina}</Pagina>"
        f"</ConsultarNFSeServicoTomadoEnvio>"
    )
    root = etree.fromstring(xml_str.encode())
    return etree.tostring(root, encoding="unicode")


def _soap_sp(inner_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
        f' xmlns:nfe="{NS_SP}">'
        "<soap:Body>"
        "<nfe:ConsultarNFSeServicoTomado>"
        "<nfe:VersaoSchema>1</nfe:VersaoSchema>"
        f"<nfe:MensagemXML>{inner_xml}</nfe:MensagemXML>"
        "</nfe:ConsultarNFSeServicoTomado>"
        "</soap:Body>"
        "</soap:Envelope>"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Parser da resposta SP
# ---------------------------------------------------------------------------

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19])
    except Exception:
        return None


def _txt(el, tag: str) -> Optional[str]:
    found = el.find(f".//{{{NS_SP}}}{tag}")
    return found.text.strip() if found is not None and found.text else None


def _parse_nfse_sp(nfse_el) -> dict:
    numero  = _txt(nfse_el, "NumeroNFe") or _txt(nfse_el, "Numero") or ""
    codigo  = _txt(nfse_el, "CodigoVerificacao") or ""
    chave   = f"SP-{numero}-{codigo}" if numero else codigo
    cnpj_e  = _txt(nfse_el, "CNPJ") or _txt(nfse_el, "CPF") or ""
    nome_e  = _txt(nfse_el, "RazaoSocial") or _txt(nfse_el, "NomeFantasia") or ""
    im_e    = _txt(nfse_el, "InscricaoMunicipal") or ""
    valor   = _txt(nfse_el, "ValorServicos") or _txt(nfse_el, "ValorLiquido") or "0"
    disc    = _txt(nfse_el, "Discriminacao") or ""
    dt_em   = _parse_dt(_txt(nfse_el, "DataEmissaoNFe") or _txt(nfse_el, "DataEmissao"))
    return {
        "chave": chave,
        "tipo": "nfse",
        "numero": numero,
        "serie": None,
        "data_emissao": dt_em,
        "emitente_cnpj": cnpj_e,
        "emitente_razao_social": nome_e,
        "emitente_uf": "SP",
        "emitente_municipio": "São Paulo",
        "valor_total": float(valor.replace(",", ".")) if valor else None,
        "natureza_operacao": disc[:100] if disc else None,
        "status_manifestacao": "ciencia_operacao",
        "xml_content": None,
    }


def _parse_response_sp(xml_bytes: bytes) -> list[dict]:
    try:
        root = etree.fromstring(xml_bytes)
        nfse_list = root.findall(f".//{{{NS_SP}}}NFe") or root.findall(f".//{{{NS_SP}}}CompNFSe")
        return [_parse_nfse_sp(el) for el in nfse_list]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Consulta principal SP
# ---------------------------------------------------------------------------

async def consultar_nfse_sp(
    cnpj: str,
    cert_path: str,
    cert_senha: str,
    im_tomador: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
) -> list[dict]:
    """
    Consulta NFS-e recebidas como tomador no WebService SP (Nota Fiscal Paulistana).

    Parâmetros:
        cnpj        : CNPJ do tomador (destinatário)
        im_tomador  : Inscrição Municipal do tomador em SP (recomendado)
        data_ini/fim: Período no formato YYYY-MM-DD (padrão: mês atual)
    """
    hoje = date.today()
    if not data_ini:
        data_ini = hoje.replace(day=1).isoformat()
    if not data_fim:
        data_fim = hoje.isoformat()

    key, cert = _load_pfx(cert_path, cert_senha)
    cert_tmp, key_tmp = _pem_files(key, cert)

    todos: list[dict] = []
    pagina = 1

    try:
        async with httpx.AsyncClient(cert=(cert_tmp, key_tmp), verify=True, timeout=60.0) as http:
            while True:
                inner_xml = _build_consulta_sp(cnpj, im_tomador, data_ini, data_fim, pagina)
                soap      = _soap_sp(inner_xml)

                resp = await http.post(
                    SP_WS_URL,
                    content=soap,
                    headers={
                        "Content-Type": "text/xml; charset=utf-8",
                        "SOAPAction": SP_SOAP_ACT,
                    },
                )
                if resp.status_code != 200:
                    break

                items = _parse_response_sp(resp.content)
                todos.extend(items)

                if len(items) < 50:
                    break
                pagina += 1
    finally:
        os.unlink(cert_tmp)
        os.unlink(key_tmp)

    return todos
