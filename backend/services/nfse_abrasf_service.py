"""
NFS-e ABRASF — cliente genérico para municípios com WebService ABRASF v2.

Opera a operação ConsultarNfseServicoTomado: retorna NFS-e onde o CNPJ
informado é o TOMADOR (destinatário) do serviço.

O WSDL varia por município; a URL é fornecida pelo banco municipios_nfse.py.

Referência: ABRASF NFS-e Serviços v2.01 e v2.04.
"""
import base64
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

NS_ABRASF = "http://www.abrasf.org.br/nfse.xsd"
NS_SIG    = "http://www.w3.org/2000/09/xmldsig#"
NS_SOAP   = "http://schemas.xmlsoap.org/soap/envelope/"


# ---------------------------------------------------------------------------
# Certificado
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
# Assinatura XML (reutiliza lógica idêntica ao sefaz_dfe_service)
# ---------------------------------------------------------------------------

def _sign_element(root, key, cert, ref_id: str) -> None:
    """Assina enveloped com RSA-SHA256; insere Signature como filho do root."""
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
# Monta payload ConsultarNfseServicoTomado
# ---------------------------------------------------------------------------

def _build_consulta_tomado(cnpj: str, im: Optional[str],
                            data_ini: str, data_fim: str, pagina: int = 1) -> bytes:
    """Monta e assina o XML ConsultarNfseServicoTomadoEnvio v2."""
    cnpj_dig = "".join(c for c in cnpj if c.isdigit())
    root = etree.fromstring(
        f'<ConsultarNfseServicoTomadoEnvio xmlns="{NS_ABRASF}">'
        f"<Tomador><CpfCnpj><Cnpj>{cnpj_dig}</Cnpj></CpfCnpj>"
        + (f"<InscricaoMunicipal>{im}</InscricaoMunicipal>" if im else "")
        + "</Tomador>"
        f"<PeriodoEmissao><DataInicial>{data_ini}</DataInicial>"
        f"<DataFinal>{data_fim}</DataFinal></PeriodoEmissao>"
        f"<Pagina>{pagina}</Pagina>"
        "</ConsultarNfseServicoTomadoEnvio>"
    )
    return etree.tostring(root, encoding="unicode")


def _soap_envelope(body_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:nfse="http://www.abrasf.org.br/nfse.xsd">'
        "<soapenv:Body>"
        "<nfse:ConsultarNfseServicoTomado>"
        "<nfse:ConsultarNfseServicoTomadoEnvioMsg>"
        f"{body_xml}"
        "</nfse:ConsultarNfseServicoTomadoEnvioMsg>"
        "</nfse:ConsultarNfseServicoTomado>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Parser da resposta
# ---------------------------------------------------------------------------

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19])
    except Exception:
        return None


def _txt(el, tag: str) -> Optional[str]:
    found = el.find(f".//{{{NS_ABRASF}}}{tag}")
    return found.text.strip() if found is not None and found.text else None


def _parse_nfse_el(nfse_el) -> dict:
    """Extrai campos de um elemento <CompNfse> ou <Nfse>."""
    numero   = _txt(nfse_el, "Numero")
    codigo   = _txt(nfse_el, "CodigoVerificacao")
    chave    = f"{numero}-{codigo}" if numero and codigo else (numero or codigo or "")
    cnpj_e   = _txt(nfse_el, "Cnpj") or _txt(nfse_el, "Cpf") or ""
    nome_e   = _txt(nfse_el, "RazaoSocial") or _txt(nfse_el, "Nome") or ""
    uf_e     = _txt(nfse_el, "Uf") or ""
    mun_e    = _txt(nfse_el, "xMunicipio") or _txt(nfse_el, "Municipio") or ""
    valor    = _txt(nfse_el, "ValorLiquidoNfse") or _txt(nfse_el, "ValorServicos") or "0"
    disc     = _txt(nfse_el, "Discriminacao") or ""
    dt_emiss = _parse_dt(_txt(nfse_el, "DataEmissao"))
    return {
        "chave": chave,
        "tipo": "nfse",
        "numero": numero,
        "serie": None,
        "data_emissao": dt_emiss,
        "emitente_cnpj": cnpj_e,
        "emitente_razao_social": nome_e,
        "emitente_uf": uf_e,
        "emitente_municipio": mun_e,
        "valor_total": float(valor.replace(",", ".")) if valor else None,
        "natureza_operacao": disc[:100] if disc else None,
        "status_manifestacao": "ciencia_operacao",
        "xml_content": None,
    }


def _parse_response(xml_bytes: bytes) -> list[dict]:
    try:
        root = etree.fromstring(xml_bytes)
        nfse_list = root.findall(f".//{{{NS_ABRASF}}}CompNfse")
        if not nfse_list:
            nfse_list = root.findall(f".//{{{NS_ABRASF}}}Nfse")
        return [_parse_nfse_el(el) for el in nfse_list]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Consulta principal
# ---------------------------------------------------------------------------

async def consultar_nfse_abrasf(
    wsdl_url: str,
    cnpj: str,
    cert_path: str,
    cert_senha: str,
    im: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
    producao: bool = True,
) -> list[dict]:
    """
    Consulta NFS-e recebidas como tomador via WebService ABRASF.

    Parâmetros:
        wsdl_url  : URL do WebService ABRASF do município (sem ?wsdl)
        cnpj      : CNPJ do tomador
        im        : Inscrição Municipal do tomador (opcional)
        data_ini  : Data inicial no formato YYYY-MM-DD (padrão: 1º do mês atual)
        data_fim  : Data final no formato YYYY-MM-DD (padrão: hoje)

    Retorna lista de dicts com campos normalizados.
    """
    from datetime import date
    hoje = date.today()
    if not data_ini:
        data_ini = hoje.replace(day=1).isoformat()
    if not data_fim:
        data_fim = hoje.isoformat()

    # Remove ?wsdl da URL se presente
    endpoint = wsdl_url.replace("?wsdl", "").replace("?WSDL", "").rstrip("/")

    key, cert = _load_pfx(cert_path, cert_senha)
    cert_tmp, key_tmp = _pem_files(key, cert)

    todos: list[dict] = []
    pagina = 1

    try:
        async with httpx.AsyncClient(cert=(cert_tmp, key_tmp), verify=False, timeout=60.0) as http:
            while True:
                consulta_xml = _build_consulta_tomado(cnpj, im, data_ini, data_fim, pagina)
                soap = _soap_envelope(consulta_xml)

                resp = await http.post(
                    endpoint,
                    content=soap,
                    headers={
                        "Content-Type": "text/xml; charset=utf-8",
                        "SOAPAction": "ConsultarNfseServicoTomado",
                    },
                )
                if resp.status_code != 200:
                    break

                items = _parse_response(resp.content)
                todos.extend(items)

                if len(items) < 50:  # ABRASF retorna max 50 por página
                    break
                pagina += 1
    finally:
        os.unlink(cert_tmp)
        os.unlink(key_tmp)

    return todos
