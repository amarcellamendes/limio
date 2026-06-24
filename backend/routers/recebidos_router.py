"""
Documentos Recebidos — NF-e, NFS-e e NFC-e recebidos pelos clientes.

Origens suportadas:
- manual: upload de XML feito pelo usuário
- sefaz_dfe: monitoramento automático via SEFAZ Distribuição DF-e (exige certificado A1)
- nfeio: webhook/polling NFe.io (exige nfse_api_key + nfse_company_id configurados)
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import Optional, List
from datetime import datetime
import xml.etree.ElementTree as ET
import httpx

from ..database import get_db
from ..models import DocumentoRecebido, Cliente, Escritorio, TipoNotaEnum
from ..auth import get_escritorio_atual, get_usuario_atual, Usuario

router = APIRouter(prefix="/api/recebidos", tags=["Documentos Recebidos"])


# ---------------------------------------------------------------------------
# Dashboard / KPIs
# ---------------------------------------------------------------------------

@router.get("/dashboard")
async def dashboard_recebidos(
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    hoje = datetime.utcnow()
    mes_atual = f"{hoje.year}-{hoje.month:02d}"

    total_res = await db.execute(
        select(func.count(DocumentoRecebido.id)).where(
            DocumentoRecebido.escritorio_id == escritorio.id
        )
    )
    total = total_res.scalar() or 0

    mes_res = await db.execute(
        select(
            DocumentoRecebido.tipo,
            func.count(DocumentoRecebido.id).label("qtd"),
            func.sum(DocumentoRecebido.valor_total).label("valor"),
        )
        .where(
            and_(
                DocumentoRecebido.escritorio_id == escritorio.id,
                DocumentoRecebido.criado_em >= f"{mes_atual}-01",
            )
        )
        .group_by(DocumentoRecebido.tipo)
    )
    por_tipo = {row.tipo: {"qtd": row.qtd, "valor": round(row.valor or 0, 2)} for row in mes_res}

    pendentes_res = await db.execute(
        select(func.count(DocumentoRecebido.id)).where(
            and_(
                DocumentoRecebido.escritorio_id == escritorio.id,
                DocumentoRecebido.tipo == TipoNotaEnum.nfe,
                DocumentoRecebido.status_manifestacao == "pendente",
            )
        )
    )
    pendentes_manifestacao = pendentes_res.scalar() or 0

    return {
        "total_geral": total,
        "mes_atual": mes_atual,
        "por_tipo": por_tipo,
        "pendentes_manifestacao_nfe": pendentes_manifestacao,
    }


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------

@router.get("")
async def listar_recebidos(
    cliente_id: Optional[int] = Query(None),
    tipo: Optional[str] = Query(None),
    origem: Optional[str] = Query(None),
    mes: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    filters = [DocumentoRecebido.escritorio_id == escritorio.id]
    if cliente_id:
        filters.append(DocumentoRecebido.cliente_id == cliente_id)
    if tipo:
        filters.append(DocumentoRecebido.tipo == tipo)
    if origem:
        filters.append(DocumentoRecebido.origem == origem)
    if mes:
        filters.append(DocumentoRecebido.criado_em >= f"{mes}-01")
        ano, m = mes.split("-")
        prox = f"{ano}-{int(m)+1:02d}-01" if int(m) < 12 else f"{int(ano)+1}-01-01"
        filters.append(DocumentoRecebido.criado_em < prox)

    result = await db.execute(
        select(DocumentoRecebido)
        .where(and_(*filters))
        .order_by(DocumentoRecebido.criado_em.desc())
        .limit(200)
    )
    docs = result.scalars().all()

    saida = []
    for d in docs:
        cli_res = await db.execute(
            select(Cliente.razao_social, Cliente.cnpj).where(Cliente.id == d.cliente_id)
        )
        cli = cli_res.one_or_none()
        saida.append({
            "id": d.id,
            "tipo": d.tipo,
            "origem": d.origem,
            "chave_acesso": d.chave_acesso,
            "numero": d.numero,
            "serie": d.serie,
            "data_emissao": d.data_emissao.isoformat() if d.data_emissao else None,
            "data_entrada": d.data_entrada.isoformat() if d.data_entrada else None,
            "emitente_cnpj": d.emitente_cnpj,
            "emitente_razao_social": d.emitente_razao_social,
            "emitente_uf": d.emitente_uf,
            "valor_total": d.valor_total,
            "status_manifestacao": d.status_manifestacao,
            "natureza_operacao": d.natureza_operacao,
            "observacoes": d.observacoes,
            "cliente_id": d.cliente_id,
            "cliente_razao_social": cli.razao_social if cli else None,
            "cliente_cnpj": cli.cnpj if cli else None,
        })
    return saida


# ---------------------------------------------------------------------------
# Upload manual de XML
# ---------------------------------------------------------------------------

@router.post("/upload")
async def upload_xml(
    cliente_id: int = Form(...),
    xml_file: UploadFile = File(...),
    observacoes: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    cli_res = await db.execute(
        select(Cliente).where(
            Cliente.id == cliente_id,
            Cliente.escritorio_id == escritorio.id
        )
    )
    cliente = cli_res.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado")

    xml_bytes = await xml_file.read()
    xml_str = xml_bytes.decode("utf-8", errors="replace")

    doc = _parse_xml_documento(xml_str)
    doc.update({
        "escritorio_id": escritorio.id,
        "cliente_id": cliente_id,
        "origem": "manual",
        "xml_content": xml_str,
        "observacoes": observacoes,
    })

    # Verifica duplicidade pela chave de acesso
    if doc.get("chave_acesso"):
        dup = await db.execute(
            select(DocumentoRecebido.id).where(
                DocumentoRecebido.escritorio_id == escritorio.id,
                DocumentoRecebido.chave_acesso == doc["chave_acesso"],
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(409, "Documento já importado (chave de acesso duplicada)")

    novo = DocumentoRecebido(**doc)
    db.add(novo)
    await db.commit()
    await db.refresh(novo)
    return {"id": novo.id, "mensagem": "Documento importado com sucesso", **doc}


# ---------------------------------------------------------------------------
# Sincronização automática — NFe.io
# ---------------------------------------------------------------------------

@router.post("/sincronizar/{cliente_id}")
async def sincronizar_cliente(
    cliente_id: int,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    cli_res = await db.execute(
        select(Cliente).where(
            Cliente.id == cliente_id,
            Cliente.escritorio_id == escritorio.id
        )
    )
    cliente = cli_res.scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente não encontrado")

    importados = 0

    # --- NFe.io: consulta notas recebidas
    if cliente.nfse_provider == "nfeio" and cliente.nfse_api_key and cliente.nfse_company_id:
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.get(
                    f"https://api.nfe.io/v1/companies/{cliente.nfse_company_id}/purchaseinvoices",
                    headers={"Authorization": f"Basic {cliente.nfse_api_key}"},
                    params={"pageSize": 50},
                )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", data) if isinstance(data, dict) else data
                for item in (items if isinstance(items, list) else []):
                    chave = item.get("accessKey") or item.get("id") or ""
                    if chave:
                        dup = await db.execute(
                            select(DocumentoRecebido.id).where(
                                DocumentoRecebido.escritorio_id == escritorio.id,
                                DocumentoRecebido.chave_acesso == chave,
                            )
                        )
                        if dup.scalar_one_or_none():
                            continue
                    novo = DocumentoRecebido(
                        escritorio_id=escritorio.id,
                        cliente_id=cliente_id,
                        tipo=TipoNotaEnum.nfe,
                        origem="nfeio",
                        chave_acesso=chave or None,
                        numero=str(item.get("number", "")),
                        serie=str(item.get("serie", "")),
                        data_emissao=_parse_dt(item.get("issuedOn")),
                        emitente_cnpj=item.get("provider", {}).get("federalTaxNumber"),
                        emitente_razao_social=item.get("provider", {}).get("name"),
                        valor_total=float(item.get("servicesAmount") or item.get("totalAmount") or 0),
                        status_manifestacao="ciencia_operacao",
                    )
                    db.add(novo)
                    importados += 1
                await db.commit()
        except Exception as e:
            raise HTTPException(502, f"Erro ao consultar NFe.io: {str(e)}")

    # --- SEFAZ DF-e: requer certificado A1 (não disponível em MOCK_MODE)
    else:
        from ..config import settings
        if settings.MOCK_MODE:
            # Simula importação de 3 notas demo
            import random
            for i in range(3):
                chave = f"{''.join([str(random.randint(0,9)) for _ in range(44)])}"
                novo = DocumentoRecebido(
                    escritorio_id=escritorio.id,
                    cliente_id=cliente_id,
                    tipo=TipoNotaEnum.nfe if i < 2 else TipoNotaEnum.nfse,
                    origem="sefaz_dfe",
                    chave_acesso=chave,
                    numero=f"{1000 + i}",
                    serie="1",
                    data_emissao=datetime.utcnow(),
                    emitente_cnpj=f"11.222.333/000{i+1}-00",
                    emitente_razao_social=f"Fornecedor Demo {i+1} Ltda",
                    emitente_uf="AM",
                    emitente_municipio="Manaus",
                    valor_total=round(500 + random.random() * 9500, 2),
                    natureza_operacao="Venda de mercadorias",
                    status_manifestacao="pendente",
                )
                db.add(novo)
                importados += 1
            await db.commit()
            return {
                "importados": importados,
                "mensagem": f"{importados} documentos importados (modo mock)",
                "aviso": "Para sincronização real via SEFAZ DF-e, configure o certificado A1 do cliente.",
            }
        else:
            raise HTTPException(
                501,
                "Sincronização SEFAZ DF-e exige certificado A1. "
                "Configure nfe_certificado_path e nfe_certificado_senha no cadastro do cliente."
            )

    return {"importados": importados, "mensagem": f"{importados} documentos importados"}


# ---------------------------------------------------------------------------
# Manifestação do destinatário (NF-e)
# ---------------------------------------------------------------------------

@router.post("/{doc_id}/manifestar")
async def manifestar(
    doc_id: int,
    tipo_evento: str = Form(...),  # ciencia_operacao | confirmacao_operacao | desconhecimento | operacao_nao_realizada
    justificativa: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    doc_res = await db.execute(
        select(DocumentoRecebido).where(
            DocumentoRecebido.id == doc_id,
            DocumentoRecebido.escritorio_id == escritorio.id,
        )
    )
    doc = doc_res.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Documento não encontrado")
    if doc.tipo != TipoNotaEnum.nfe:
        raise HTTPException(400, "Manifestação disponível apenas para NF-e")

    eventos_validos = {
        "ciencia_operacao", "confirmacao_operacao",
        "desconhecimento", "operacao_nao_realizada",
    }
    if tipo_evento not in eventos_validos:
        raise HTTPException(400, f"Evento inválido. Use: {eventos_validos}")

    doc.status_manifestacao = tipo_evento
    if justificativa:
        doc.observacoes = (doc.observacoes or "") + f"\n[Manifestação: {tipo_evento}] {justificativa}"
    await db.commit()

    labels = {
        "ciencia_operacao": "Ciência da Operação",
        "confirmacao_operacao": "Confirmação da Operação",
        "desconhecimento": "Desconhecimento da Operação",
        "operacao_nao_realizada": "Operação não Realizada",
    }
    return {"mensagem": f"Manifestação registrada: {labels[tipo_evento]}", "status": tipo_evento}


# ---------------------------------------------------------------------------
# Download XML
# ---------------------------------------------------------------------------

@router.get("/{doc_id}/xml")
async def download_xml(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    doc_res = await db.execute(
        select(DocumentoRecebido).where(
            DocumentoRecebido.id == doc_id,
            DocumentoRecebido.escritorio_id == escritorio.id,
        )
    )
    doc = doc_res.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Documento não encontrado")
    if not doc.xml_content:
        raise HTTPException(404, "XML não disponível")
    return Response(
        content=doc.xml_content.encode("utf-8"),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="doc_{doc_id}.xml"'},
    )


# ---------------------------------------------------------------------------
# Exclusão
# ---------------------------------------------------------------------------

@router.delete("/{doc_id}")
async def excluir(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    escritorio: Escritorio = Depends(get_escritorio_atual),
):
    doc_res = await db.execute(
        select(DocumentoRecebido).where(
            DocumentoRecebido.id == doc_id,
            DocumentoRecebido.escritorio_id == escritorio.id,
        )
    )
    doc = doc_res.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Documento não encontrado")
    await db.delete(doc)
    await db.commit()
    return {"mensagem": "Documento excluído"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_xml_documento(xml_str: str) -> dict:
    """Extrai campos comuns de XML NF-e, NFC-e ou NFS-e."""
    out = {
        "tipo": TipoNotaEnum.nfe,
        "chave_acesso": None,
        "numero": None,
        "serie": None,
        "data_emissao": None,
        "emitente_cnpj": None,
        "emitente_razao_social": None,
        "emitente_uf": None,
        "emitente_municipio": None,
        "valor_total": None,
        "natureza_operacao": None,
        "status_manifestacao": "pendente",
    }
    try:
        root = ET.fromstring(xml_str)
        ns = {"nfe": "http://www.portalfiscal.inf.br/nfe", "nfse": "http://www.abrasf.org.br/nfse.xsd"}

        def find(path, nsmap=None):
            for el in (root.iter(path.split(":")[-1]) if ":" not in path else []):
                return el.text
            for prefix, uri in (nsmap or {}).items():
                el = root.find(f".//{{{uri}}}{path.split(':')[-1]}")
                if el is not None and el.text:
                    return el.text
            return None

        # NF-e / NFC-e (modelo 55/65)
        mod = find("mod") or find("mod", ns)
        if mod == "65":
            out["tipo"] = TipoNotaEnum.nfce
            out["status_manifestacao"] = "confirmacao_operacao"
        else:
            out["tipo"] = TipoNotaEnum.nfe

        chave = find("Id") or find("chNFe")
        if chave:
            out["chave_acesso"] = chave.replace("NFe", "")[:44]

        out["numero"] = find("nNF") or find("numero")
        out["serie"] = find("serie")
        out["natureza_operacao"] = find("natOp")

        dt_raw = find("dhEmi") or find("dEmi")
        out["data_emissao"] = _parse_dt(dt_raw)

        out["emitente_cnpj"] = find("CNPJ") or find("CPF")
        out["emitente_razao_social"] = find("xNome")
        out["emitente_uf"] = find("UF") or find("cUF")
        out["emitente_municipio"] = find("xMun")

        vt = find("vNF") or find("vTotServ") or find("ValorServicos")
        if vt:
            try:
                out["valor_total"] = float(vt)
            except Exception:
                pass

        # NFS-e
        if "CompNfse" in xml_str or "Nfse" in xml_str:
            out["tipo"] = TipoNotaEnum.nfse
            out["status_manifestacao"] = "confirmacao_operacao"

    except Exception:
        pass

    return out
