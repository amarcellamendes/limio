"""Geração de PDF para NFS-e e NF-e usando ReportLab."""

import io
import base64
from datetime import datetime
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

# Cores do sistema
AZUL = colors.HexColor("#013957")
DOURADO = colors.HexColor("#d88d2a")
CREME = colors.HexColor("#fff1e2")
CINZA_CLARO = colors.HexColor("#f5f5f5")


def gerar_pdf_nfse(nota_data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    story = []

    # Cabeçalho
    titulo_style = ParagraphStyle(
        "titulo", parent=styles["Normal"],
        fontSize=16, textColor=AZUL, alignment=TA_CENTER,
        fontName="Helvetica-Bold", spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "sub", parent=styles["Normal"],
        fontSize=10, textColor=colors.grey, alignment=TA_CENTER,
        spaceAfter=2,
    )
    label_style = ParagraphStyle(
        "label", parent=styles["Normal"],
        fontSize=8, textColor=AZUL, fontName="Helvetica-Bold",
    )
    valor_style = ParagraphStyle(
        "valor", parent=styles["Normal"],
        fontSize=9, textColor=colors.black,
    )
    valor_grande_style = ParagraphStyle(
        "valor_grande", parent=styles["Normal"],
        fontSize=14, textColor=AZUL, fontName="Helvetica-Bold",
        alignment=TA_RIGHT,
    )

    story.append(Paragraph("NOTA FISCAL DE SERVIÇOS ELETRÔNICA", titulo_style))
    story.append(Paragraph("NFS-e", sub_style))
    story.append(HRFlowable(width="100%", thickness=2, color=DOURADO))
    story.append(Spacer(1, 4 * mm))

    # Número e status
    numero = nota_data.get("numero", "—")
    status = nota_data.get("status", "emitida").upper()
    status_color = AZUL if status == "EMITIDA" else colors.red

    header_data = [
        [
            Paragraph(f"<b>Nº {numero}</b>", ParagraphStyle("n", fontSize=18, textColor=AZUL, fontName="Helvetica-Bold")),
            Paragraph(f"<b>{status}</b>", ParagraphStyle("s", fontSize=14, textColor=status_color, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
        ]
    ]
    header_table = Table(header_data, colWidths=["60%", "40%"])
    story.append(header_table)
    story.append(Spacer(1, 2 * mm))

    # Data e competência
    data_emissao = nota_data.get("data_emissao", "")
    if isinstance(data_emissao, str) and data_emissao:
        try:
            data_emissao = datetime.fromisoformat(data_emissao[:19]).strftime("%d/%m/%Y %H:%M")
        except Exception:
            pass

    info_data = [
        ["Data de Emissão", "Competência", "Código de Verificação"],
        [
            str(data_emissao or "—"),
            nota_data.get("data_competencia", "—"),
            nota_data.get("chave_acesso", nota_data.get("provider_id", "—"))[:30],
        ],
    ]
    info_table = Table(info_data, colWidths=["33%", "33%", "34%"])
    info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTSIZE", (0, 1), (-1, 1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [CREME]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 4 * mm))

    # Prestador e Tomador
    story.append(Paragraph("PRESTADOR DE SERVIÇOS", label_style))
    story.append(Spacer(1, 1 * mm))
    prestador_data = [
        [
            Paragraph(f"<b>{nota_data.get('cliente_razao_social', 'N/A')}</b>", valor_style),
            Paragraph(f"CNPJ: {_formatar_cnpj(nota_data.get('cliente_cnpj', ''))}", valor_style),
        ],
        [
            Paragraph(f"{nota_data.get('cliente_municipio', '')} - {nota_data.get('cliente_uf', '')}", valor_style),
            Paragraph(f"IM: {nota_data.get('cliente_im', '—')}", valor_style),
        ],
    ]
    prest_table = Table(prestador_data, colWidths=["65%", "35%"])
    prest_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CINZA_CLARO),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(prest_table)
    story.append(Spacer(1, 3 * mm))

    story.append(Paragraph("TOMADOR DE SERVIÇOS", label_style))
    story.append(Spacer(1, 1 * mm))
    tomador_data = [
        [
            Paragraph(f"<b>{nota_data.get('tomador_razao_social', 'N/A')}</b>", valor_style),
            Paragraph(f"CPF/CNPJ: {_formatar_cpf_cnpj(nota_data.get('tomador_cpf_cnpj', ''))}", valor_style),
        ],
        [
            Paragraph(f"{nota_data.get('tomador_municipio', '')} - {nota_data.get('tomador_uf', '')} | CEP: {nota_data.get('tomador_cep', '—')}", valor_style),
            Paragraph(f"E-mail: {nota_data.get('tomador_email', '—')}", valor_style),
        ],
    ]
    tom_table = Table(tomador_data, colWidths=["65%", "35%"])
    tom_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CINZA_CLARO),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tom_table)
    story.append(Spacer(1, 4 * mm))

    # Discriminação
    story.append(Paragraph("DISCRIMINAÇÃO DOS SERVIÇOS", label_style))
    story.append(Spacer(1, 1 * mm))
    disc_style = ParagraphStyle(
        "disc", parent=styles["Normal"],
        fontSize=9, leading=14, leftIndent=5, rightIndent=5,
    )
    disc_box = Table(
        [[Paragraph(nota_data.get("discriminacao", "—"), disc_style)]],
        colWidths=["100%"],
    )
    disc_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CINZA_CLARO),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(disc_box)
    story.append(Spacer(1, 4 * mm))

    # Código de serviço
    story.append(Paragraph("SERVIÇO", label_style))
    story.append(Spacer(1, 1 * mm))
    serv_data = [
        ["Código LC 116", "Código Municipal", "ISS Retido"],
        [
            nota_data.get("codigo_servico_lc116", "—"),
            nota_data.get("codigo_servico_municipal", "—"),
            "Sim" if nota_data.get("iss_retido") else "Não",
        ],
    ]
    serv_table = Table(serv_data, colWidths=["33%", "33%", "34%"])
    serv_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTSIZE", (0, 1), (-1, 1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [CREME]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(serv_table)
    story.append(Spacer(1, 4 * mm))

    # Valores
    story.append(Paragraph("VALORES", label_style))
    story.append(Spacer(1, 1 * mm))

    def _fmt(v: Optional[float]) -> str:
        if v is None or v == 0:
            return "R$ 0,00"
        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    aliq = nota_data.get("aliquota_iss") or 0
    valores_data = [
        ["Descrição", "Valor"],
        ["Valor dos Serviços", _fmt(nota_data.get("valor_servico"))],
        ["(-) Deduções", _fmt(nota_data.get("valor_deducoes", 0))],
        [f"(-) ISS ({aliq:.2f}%)", _fmt(nota_data.get("valor_iss", 0))],
        ["(-) PIS", _fmt(nota_data.get("valor_pis", 0))],
        ["(-) COFINS", _fmt(nota_data.get("valor_cofins", 0))],
        ["(-) INSS", _fmt(nota_data.get("valor_inss", 0))],
        ["(-) IR", _fmt(nota_data.get("valor_ir", 0))],
        ["(-) CSLL", _fmt(nota_data.get("valor_csll", 0))],
        ["VALOR LÍQUIDO", _fmt(nota_data.get("valor_liquido"))],
    ]
    val_table = Table(valores_data, colWidths=["70%", "30%"])
    val_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), AZUL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BACKGROUND", (0, -1), (-1, -1), DOURADO),
        ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 11),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, CINZA_CLARO]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(val_table)
    story.append(Spacer(1, 6 * mm))

    # Rodapé
    rodape_style = ParagraphStyle(
        "rodape", parent=styles["Normal"],
        fontSize=7, textColor=colors.grey, alignment=TA_CENTER,
    )
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(
        "Documento gerado eletronicamente. Consulte a autenticidade no portal da prefeitura.",
        rodape_style,
    ))
    story.append(Paragraph(
        f"Gerado em: {datetime.now().strftime('%d/%m/%Y às %H:%M')} | "
        f"Provider: {nota_data.get('provider', '—').upper()}",
        rodape_style,
    ))

    doc.build(story)
    return buffer.getvalue()


def gerar_pdf_nfe(nota_data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=15*mm, leftMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    story = []

    titulo_style = ParagraphStyle("t", fontSize=14, textColor=AZUL, alignment=TA_CENTER,
                                   fontName="Helvetica-Bold", spaceAfter=4)
    story.append(Paragraph("DANFE — DOCUMENTO AUXILIAR DA NOTA FISCAL ELETRÔNICA", titulo_style))
    story.append(Paragraph("Modelo 55 | SIMULAÇÃO / HOMOLOGAÇÃO", ParagraphStyle(
        "s", fontSize=9, textColor=colors.red, alignment=TA_CENTER, spaceAfter=4)))
    story.append(HRFlowable(width="100%", thickness=2, color=DOURADO))
    story.append(Spacer(1, 4*mm))

    info = [
        ["Chave de Acesso", nota_data.get("chave_acesso", "—")],
        ["Número / Série", f"{nota_data.get('numero','—')} / {nota_data.get('serie','1')}"],
        ["Natureza da Operação", nota_data.get("natureza_operacao", "—")],
        ["Data Emissão", str(nota_data.get("data_emissao", "—"))[:10]],
        ["Emitente", nota_data.get("cliente_razao_social", "—")],
        ["Destinatário", nota_data.get("tomador_razao_social", "—")],
        ["Valor Total NF-e", f"R$ {nota_data.get('valor_servico', 0):,.2f}"],
    ]
    t = Table(info, colWidths=["35%", "65%"])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [CREME, colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    doc.build(story)
    return buffer.getvalue()


def pdf_to_base64(pdf_bytes: bytes) -> str:
    return base64.b64encode(pdf_bytes).decode("utf-8")


# ─── Relatório de Apuração Mensal ─────────────────────────────────────────

def gerar_relatorio_apuracao(
    escritorio: dict,
    cliente: dict,
    competencia: str,  # "YYYY-MM"
    notas: list,
) -> bytes:
    """PDF consolidado de todas as notas emitidas no mês, com totais tributários."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=15*mm, leftMargin=15*mm, topMargin=15*mm, bottomMargin=20*mm
    )
    styles = getSampleStyleSheet()
    story = []

    titulo = ParagraphStyle("titulo", fontName="Helvetica-Bold", fontSize=14, textColor=AZUL)
    subtitulo = ParagraphStyle("sub", fontName="Helvetica-Bold", fontSize=10, textColor=DOURADO)
    corpo = ParagraphStyle("corpo", fontName="Helvetica", fontSize=8, leading=11)
    label_s = ParagraphStyle("label", fontName="Helvetica-Bold", fontSize=7,
                              textColor=colors.white, backColor=AZUL,
                              spaceAfter=2, spaceBefore=6)

    def brl(v):
        if not v: return "R$ 0,00"
        return f"R$ {float(v):,.2f}".replace(",","X").replace(".",",").replace("X",".")

    ano, mes_num = competencia.split("-")
    meses_pt = ["","Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]
    mes_nome = meses_pt[int(mes_num)]

    # ── Cabeçalho ──
    story.append(Paragraph(f"Relatório de Apuração Mensal", titulo))
    story.append(Paragraph(f"{mes_nome} / {ano}", subtitulo))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=DOURADO))
    story.append(Spacer(1, 3*mm))

    # Escritório + Cliente lado a lado
    info_data = [
        [Paragraph("<b>Escritório Contábil</b>", corpo),
         Paragraph("<b>Cliente / Empresa</b>", corpo)],
        [Paragraph(escritorio.get("nome",""), corpo),
         Paragraph(cliente.get("razao_social",""), corpo)],
        [Paragraph(f"CNPJ: {escritorio.get('cnpj','')}  CRC: {escritorio.get('crc','')}",corpo),
         Paragraph(f"CNPJ: {cliente.get('cnpj','')}",corpo)],
        [Paragraph(f"{escritorio.get('email','')}",corpo),
         Paragraph(f"{cliente.get('municipio','')} / {cliente.get('uf','')}",corpo)],
    ]
    info_table = Table(info_data, colWidths=["50%","50%"])
    info_table.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("BACKGROUND",(0,0),(-1,0),AZUL),
        ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),8),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[CREME, colors.white]),
        ("TOPPADDING",(0,0),(-1,-1),4),
        ("BOTTOMPADDING",(0,0),(-1,-1),4),
        ("LEFTPADDING",(0,0),(-1,-1),6),
        ("LINEAFTER",(0,0),(0,-1),0.5,colors.grey),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 5*mm))

    # ── Resumo de Totais ──
    story.append(Paragraph("RESUMO DE IMPOSTOS E RETENÇÕES", label_s))
    story.append(Spacer(1, 1*mm))

    nfse_list = [n for n in notas if n.get("tipo") == "nfse"]
    nfe_list  = [n for n in notas if n.get("tipo") == "nfe"]

    def soma(lst, campo):
        return sum(float(n.get(campo) or 0) for n in lst)

    total_bruto   = soma(notas, "valor_servico")
    total_iss     = soma(notas, "valor_iss")
    total_ir      = soma(notas, "valor_ir")
    total_inss    = soma(notas, "valor_inss")
    total_pis     = soma(notas, "valor_pis")
    total_cofins  = soma(notas, "valor_cofins")
    total_csll    = soma(notas, "valor_csll")
    total_retenc  = total_ir + total_inss + total_pis + total_cofins + total_csll
    total_liquido = soma(notas, "valor_liquido")

    resumo_data = [
        ["Descrição", "NFS-e", "NF-e", "Total"],
        ["Qtde de documentos", str(len(nfse_list)), str(len(nfe_list)), str(len(notas))],
        ["Valor Bruto dos Serviços", brl(soma(nfse_list,"valor_servico")),
         brl(soma(nfe_list,"valor_servico")), brl(total_bruto)],
        ["ISS Destacado", brl(soma(nfse_list,"valor_iss")), "—", brl(total_iss)],
        ["IR Retido na Fonte", brl(soma(nfse_list,"valor_ir")), brl(soma(nfe_list,"valor_ir")), brl(total_ir)],
        ["INSS Retido", brl(soma(nfse_list,"valor_inss")), "—", brl(total_inss)],
        ["PIS Retido", brl(soma(nfse_list,"valor_pis")), brl(soma(nfe_list,"valor_pis")), brl(total_pis)],
        ["COFINS Retido", brl(soma(nfse_list,"valor_cofins")), brl(soma(nfe_list,"valor_cofins")), brl(total_cofins)],
        ["CSLL Retido", brl(soma(nfse_list,"valor_csll")), brl(soma(nfe_list,"valor_csll")), brl(total_csll)],
        ["Total Retenções", "—", "—", brl(total_retenc)],
        ["Valor Líquido Recebido", brl(soma(nfse_list,"valor_liquido")),
         brl(soma(nfe_list,"valor_liquido")), brl(total_liquido)],
    ]
    res_table = Table(resumo_data, colWidths=["40%","20%","20%","20%"])
    res_table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),AZUL),
        ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),
        ("BACKGROUND",(0,-1),(-1,-1),DOURADO),
        ("TEXTCOLOR",(0,-1),(-1,-1),colors.white),
        ("BACKGROUND",(0,-2),(-1,-2),colors.HexColor("#f0fdf4")),
        ("FONTNAME",(0,-2),(-1,-2),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),8),
        ("ALIGN",(1,0),(-1,-1),"RIGHT"),
        ("ROWBACKGROUNDS",(0,1),(-1,-2),[CREME, colors.white]),
        ("GRID",(0,0),(-1,-1),0.3,colors.grey),
        ("TOPPADDING",(0,0),(-1,-1),4),
        ("BOTTOMPADDING",(0,0),(-1,-1),4),
        ("LEFTPADDING",(0,0),(-1,-1),6),
    ]))
    story.append(res_table)
    story.append(Spacer(1, 6*mm))

    # ── Detalhamento: NFS-e ──
    if nfse_list:
        story.append(Paragraph("NOTAS FISCAIS DE SERVIÇO (NFS-e) EMITIDAS", label_s))
        story.append(Spacer(1, 1*mm))
        nfse_header = ["Nº / RPS", "Data", "Tomador", "Valor Bruto", "ISS", "IR", "Líquido", "Status"]
        nfse_rows = [nfse_header]
        for n in nfse_list:
            dt = n.get("data_emissao") or ""
            if dt:
                try: dt = datetime.fromisoformat(dt[:19]).strftime("%d/%m/%y")
                except: pass
            nfse_rows.append([
                str(n.get("numero") or n.get("numero_rps") or "—"),
                dt,
                Paragraph((n.get("tomador_razao_social") or "—")[:35], corpo),
                brl(n.get("valor_servico")),
                brl(n.get("valor_iss")),
                brl(n.get("valor_ir")),
                brl(n.get("valor_liquido")),
                (n.get("status") or "").capitalize(),
            ])
        nfse_table = Table(nfse_rows, colWidths=["10%","10%","28%","12%","10%","10%","10%","10%"])
        nfse_table.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),AZUL),
            ("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
            ("FONTSIZE",(0,0),(-1,-1),7),
            ("ALIGN",(3,0),(-1,-1),"RIGHT"),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[CREME, colors.white]),
            ("GRID",(0,0),(-1,-1),0.3,colors.grey),
            ("TOPPADDING",(0,0),(-1,-1),3),
            ("BOTTOMPADDING",(0,0),(-1,-1),3),
            ("LEFTPADDING",(0,0),(-1,-1),4),
        ]))
        story.append(nfse_table)
        story.append(Spacer(1, 5*mm))

    # ── Detalhamento: NF-e ──
    if nfe_list:
        story.append(Paragraph("NOTAS FISCAIS DE PRODUTO (NF-e) EMITIDAS", label_s))
        story.append(Spacer(1, 1*mm))
        nfe_header = ["Nº NF-e", "Data", "Destinatário", "Valor Total", "Status"]
        nfe_rows = [nfe_header]
        for n in nfe_list:
            dt = n.get("data_emissao") or ""
            if dt:
                try: dt = datetime.fromisoformat(dt[:19]).strftime("%d/%m/%y")
                except: pass
            nfe_rows.append([
                str(n.get("numero") or "—"),
                dt,
                Paragraph((n.get("tomador_razao_social") or "—")[:45], corpo),
                brl(n.get("valor_servico")),
                (n.get("status") or "").capitalize(),
            ])
        nfe_table = Table(nfe_rows, colWidths=["12%","12%","50%","16%","10%"])
        nfe_table.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),AZUL),
            ("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
            ("FONTSIZE",(0,0),(-1,-1),7),
            ("ALIGN",(3,0),(3,-1),"RIGHT"),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[CREME, colors.white]),
            ("GRID",(0,0),(-1,-1),0.3,colors.grey),
            ("TOPPADDING",(0,0),(-1,-1),3),
            ("BOTTOMPADDING",(0,0),(-1,-1),3),
            ("LEFTPADDING",(0,0),(-1,-1),4),
        ]))
        story.append(nfe_table)
        story.append(Spacer(1, 5*mm))

    # ── Rodapé / Assinatura ──
    story.append(HRFlowable(width="100%", thickness=1, color=DOURADO))
    story.append(Spacer(1, 3*mm))
    assinatura_data = [[
        Paragraph(f"Emitido em: {datetime.now().strftime('%d/%m/%Y às %H:%M')}", corpo),
        Paragraph(f"<b>{escritorio.get('nome','')}</b><br/>"
                  f"CRC: {escritorio.get('crc','')}", corpo),
    ]]
    ass_table = Table(assinatura_data, colWidths=["50%","50%"])
    ass_table.setStyle(TableStyle([
        ("ALIGN",(1,0),(1,-1),"RIGHT"),
        ("FONTSIZE",(0,0),(-1,-1),7),
    ]))
    story.append(ass_table)

    doc.build(story)
    return buffer.getvalue()


def _formatar_cnpj(cnpj: str) -> str:
    d = "".join(c for c in cnpj if c.isdigit())
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return cnpj


def _formatar_cpf_cnpj(doc: str) -> str:
    d = "".join(c for c in doc if c.isdigit())
    if len(d) == 14:
        return _formatar_cnpj(d)
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return doc
