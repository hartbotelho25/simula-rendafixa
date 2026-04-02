"""
Simulador de renda fixa (educacional) — dados de mercado via API SGS do BCB.

Instalação: pip install -r requirements.txt
Execução local: streamlit run renda.py
Deploy: ver DEPLOY.md (GitHub + Streamlit Cloud).
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pandas as pd
import requests
import streamlit as st

# --- Códigos SGS (BCB) ---
SGS_SELIC_META_AA = 432  # Selic meta Copom, % a.a.
SGS_CDI_DIARIO = 12  # CDI, % ao dia (base 252)
SGS_IPCA_12M = 13522  # IPCA acumulado 12 meses, %
SGS_TR_MENSAL = 7811  # TR, % ao mês


def _parse_valor_br(valor: Any) -> float:
    return float(str(valor).strip().replace(",", "."))


@st.cache_data(ttl=3600)
def consulta_bcb(codigo: int, ultimos: int = 1) -> float:
    """Último valor numérico da série SGS (ou média dos últimos `ultimos` valores)."""
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados/ultimos/{ultimos}?formato=json"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"Série {codigo} retornou vazio.")
    valores = [_parse_valor_br(row["valor"]) for row in data]
    return sum(valores) / len(valores)


def cdi_percentual_anual(cdi_diario_percent: float) -> float:
    """CDI a.a. a partir do CDI % a.d. (252 dias úteis)."""
    return ((1 + cdi_diario_percent / 100) ** 252 - 1) * 100


def poupança_taxa_mensal_aproximada(
    selic_meta_aa_percent: float,
    tr_mensal_percent: float,
    cdi_diario_percent: float,
) -> float:
    if selic_meta_aa_percent > 8.5:
        return 0.005 + (tr_mensal_percent / 100)
    fator_dia = 1 + cdi_diario_percent / 100
    fator_mes_21du = fator_dia**21
    return 0.7 * (fator_mes_21du - 1)


def calcular_ir(prazo_dias: int, tipo_ativo: str) -> float:
    if tipo_ativo in ("Poupança", "LCI/LCA"):
        return 0.0
    if prazo_dias <= 180:
        return 0.225
    if prazo_dias <= 360:
        return 0.20
    if prazo_dias <= 720:
        return 0.175
    return 0.15


def projetar_montante(
    capital: float,
    taxa_efetiva_anual: float,
    meses: int,
    compounding_mensal: bool = True,
) -> tuple[float, float]:
    r_a = taxa_efetiva_anual
    if compounding_mensal:
        r_m = (1 + r_a) ** (1 / 12) - 1
        final = capital * (1 + r_m) ** meses
        return final, r_m
    final = capital * (1 + r_a) ** (meses / 12)
    r_m = (1 + r_a) ** (1 / 12) - 1
    return final, r_m


def taxa_bruta_anual_decimal(
    modalidade: str,
    pct_do_cdi: float,
    taxa_pre_fixada_aa: float,
    cdi_aa_percent: float,
) -> float:
    """Retorna taxa bruta anual em decimal (ex.: 0,12 para 12%)."""
    if modalidade == "pos":
        return (cdi_aa_percent * (pct_do_cdi / 100.0)) / 100.0
    return taxa_pre_fixada_aa / 100.0


def montar_linhas(
    *,
    inc_poup: bool,
    inc_cdb: bool,
    inc_cdb_pos: bool,
    inc_cdb_pre: bool,
    pct_cdb: float,
    pre_cdb: float,
    inc_lci: bool,
    inc_lci_pos: bool,
    inc_lci_pre: bool,
    pct_lci: float,
    pre_lci: float,
    inc_fundo: bool,
    pct_fundo_cdi: float,
    adm_fundo: float,
    poupanca_aa_equiv: float,
    cdi_aa: float,
) -> list[dict[str, Any]]:
    """Monta lista de ativos para simulação (mesma ordem da tabela principal)."""
    linhas: list[dict[str, Any]] = []

    if inc_poup:
        linhas.append(
            {
                "nome": "Poupança",
                "tipo": "Poupança",
                "taxa_liquida_anual": poupanca_aa_equiv,
                "detalhe": f"~{poupanca_aa_equiv * 100:.2f}% a.a. eq.",
            }
        )

    if inc_cdb and inc_cdb_pos:
        bruta = taxa_bruta_anual_decimal("pos", pct_cdb, pre_cdb, cdi_aa)
        linhas.append(
            {
                "nome": "CDB · pós",
                "tipo": "CDB",
                "modalidade": "pos",
                "taxa_bruta_anual": bruta,
                "detalhe": f"{pct_cdb:.2f}% CDI → ~{bruta * 100:.2f}% bruto",
            }
        )
    if inc_cdb and inc_cdb_pre:
        bruta = taxa_bruta_anual_decimal("pre", pct_cdb, pre_cdb, cdi_aa)
        linhas.append(
            {
                "nome": "CDB · pré",
                "tipo": "CDB",
                "modalidade": "pre",
                "taxa_bruta_anual": bruta,
                "detalhe": f"Pré {pre_cdb:.2f}% a.a.",
            }
        )

    if inc_lci and inc_lci_pos:
        bruta = taxa_bruta_anual_decimal("pos", pct_lci, pre_lci, cdi_aa)
        linhas.append(
            {
                "nome": "LCI/LCA · pós",
                "tipo": "LCI/LCA",
                "modalidade": "pos",
                "taxa_bruta_anual": bruta,
                "detalhe": f"{pct_lci:.2f}% CDI → ~{bruta * 100:.2f}% bruto",
            }
        )
    if inc_lci and inc_lci_pre:
        bruta = taxa_bruta_anual_decimal("pre", pct_lci, pre_lci, cdi_aa)
        linhas.append(
            {
                "nome": "LCI/LCA · pré",
                "tipo": "LCI/LCA",
                "modalidade": "pre",
                "taxa_bruta_anual": bruta,
                "detalhe": f"Pré {pre_lci:.2f}% a.a.",
            }
        )

    if inc_fundo:
        bruta_fundo_cdi = (cdi_aa * (pct_fundo_cdi / 100.0)) / 100.0
        bruta_fundo = max(bruta_fundo_cdi - adm_fundo / 100.0, 0.0)
        linhas.append(
            {
                "nome": "Fundo DI",
                "tipo": "CDB",
                "modalidade": "pos",
                "taxa_bruta_anual": bruta_fundo,
                "pct_fundo_cdi_ref": pct_fundo_cdi,
                "adm_fundo": adm_fundo,
                "detalhe": f"{pct_fundo_cdi:.2f}% CDI − {adm_fundo:.2f}% adm",
            }
        )

    return linhas


def info_pai_negociacao(ativo_escolhido: str) -> dict[str, Any] | None:
    """Dados mínimos do ativo para negociação quando a linha da prateleira não foi incluída."""
    m: dict[str, tuple[str, str]] = {
        "CDB · pós": ("CDB", "pos"),
        "CDB · pré": ("CDB", "pre"),
        "LCI/LCA · pós": ("LCI/LCA", "pos"),
        "LCI/LCA · pré": ("LCI/LCA", "pre"),
    }
    if ativo_escolhido not in m:
        return None
    tipo, modalidade = m[ativo_escolhido]
    return {"nome": ativo_escolhido, "tipo": tipo, "modalidade": modalidade}


def linha_negociada_de_pai(
    info_pai: dict[str, Any],
    nova_taxa_percent: float,
    meses_carencia: int,
    cdi_aa: float,
) -> dict[str, Any]:
    """Clona lógica do ativo pai com nova taxa; mantém tipo (IR / isenção) e texto de Condição como na prateleira."""
    nome_base = str(info_pai["nome"])
    suf = f"C {int(meses_carencia)}m"
    nome_exibir = f"{nome_base} {suf}"
    modalidade = str(info_pai.get("modalidade") or "pos")

    if modalidade == "pre":
        bruta = taxa_bruta_anual_decimal("pre", 0.0, nova_taxa_percent, cdi_aa)
        detalhe = f"Pré {nova_taxa_percent:.2f}% a.a."
    else:
        bruta = taxa_bruta_anual_decimal("pos", nova_taxa_percent, 0.0, cdi_aa)
        detalhe = f"{nova_taxa_percent:.2f}% CDI → ~{bruta * 100:.2f}% bruto"

    return {
        "nome": nome_exibir,
        "tipo": info_pai["tipo"],
        "modalidade": modalidade,
        "taxa_bruta_anual": bruta,
        "detalhe": detalhe,
        "condicao_negocial": True,
    }


def resultado_para_tabela(
    info: dict[str, Any],
    *,
    valor_total: float,
    prazo_meses: int,
    prazo_dias: int,
    exibir_inflacao: bool,
    ipca_12m: float,
) -> dict[str, str]:
    """Uma linha da tabela de resultados a partir do dict de ativo."""
    if "taxa_liquida_anual" in info:
        taxa_liquida_anual = float(info["taxa_liquida_anual"])
        ir_label = "0%"
    else:
        bruta = float(info["taxa_bruta_anual"])
        aliquota_ir = calcular_ir(prazo_dias, str(info["tipo"]))
        taxa_liquida_anual = bruta * (1 - aliquota_ir)
        ir_label = f"{aliquota_ir * 100:.1f}%"

    if exibir_inflacao:
        taxa_exibida = (1 + taxa_liquida_anual) / (1 + ipca_12m / 100) - 1
    else:
        taxa_exibida = taxa_liquida_anual

    valor_final, taxa_mensal = projetar_montante(valor_total, taxa_exibida, prazo_meses)
    recebido = valor_final - valor_total

    nome_col = str(info["nome"])
    if info.get("condicao_negocial"):
        nome_col = f"✨ {nome_col}"

    return {
        "Ativo": nome_col,
        "Condição": str(info.get("detalhe", "")),
        "IR": ir_label,
        "% mês": f"{taxa_mensal * 100:.3f}",
        "% a.a.": f"{taxa_exibida * 100:.2f}",
        "Montante": f"R$ {format_moeda_br(valor_final)}",
        "Recebido": f"R$ {format_moeda_br(recebido)}",
    }


def format_moeda_br(valor: float) -> str:
    """Exibe valor com milhar em ponto e centavos após vírgula (ex.: 10.000,00)."""
    neg = valor < 0
    v = abs(float(valor))
    centavos = int(round(v * 100))
    inteiro = centavos // 100
    centavos = centavos % 100
    s_int = f"{inteiro:,}".replace(",", ".")
    out = f"{s_int},{centavos:02d}"
    return f"-{out}" if neg else out


def parse_moeda_br(texto: str) -> float:
    """Interpreta texto com milhar '.' e decimal ',' (estilo BR)."""
    s = (texto or "").strip().replace(" ", "").replace("\u00a0", "")
    if not s:
        return 0.0
    if "," in s:
        partes = s.rsplit(",", 1)
        if len(partes) == 2 and partes[1].isdigit() and len(partes[1]) <= 2:
            inteiro_txt = partes[0].replace(".", "")
            return float(f"{inteiro_txt}.{partes[1]}")
    return float(s.replace(".", "").replace(",", "."))


_fontes_reportlab_cache: tuple[str, str] | None = None


def _fontes_reportlab() -> tuple[str, str]:
    """Registra fonte TTF com suporte a português; retorna (normal, negrito)."""
    global _fontes_reportlab_cache
    if _fontes_reportlab_cache is not None:
        return _fontes_reportlab_cache

    from pathlib import Path
    import os

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pares: list[tuple[Path, Path]] = []
    if os.name == "nt":
        w = Path(os.environ.get("WINDIR", "C:/Windows"))
        pares.append((w / "Fonts" / "arial.ttf", w / "Fonts" / "arialbd.ttf"))
        pares.append((w / "Fonts" / "calibri.ttf", w / "Fonts" / "calibrib.ttf"))
    pares.append(
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        )
    )
    for reg, negrito in pares:
        try:
            if reg.is_file():
                pdfmetrics.registerFont(TTFont("RFBody", str(reg)))
                if negrito.is_file():
                    pdfmetrics.registerFont(TTFont("RFBodyBold", str(negrito)))
                else:
                    pdfmetrics.registerFont(TTFont("RFBodyBold", str(reg)))
                _fontes_reportlab_cache = ("RFBody", "RFBodyBold")
                return _fontes_reportlab_cache
        except (OSError, ValueError, KeyError):
            continue
    _fontes_reportlab_cache = ("Helvetica", "Helvetica-Bold")
    return _fontes_reportlab_cache


def gerar_pdf_resultados(
    df: pd.DataFrame,
    *,
    prazo_meses: int,
    prazo_dias: int,
    valor_total: float,
    exibir_inflacao: bool,
    selic_meta_aa: float,
    cdi_aa: float,
    ipca_12m: float,
    tr_m: float,
) -> bytes:
    """Monta PDF A4 com título, metadados e tabela (apenas linhas selecionadas)."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font_n, font_b = _fontes_reportlab()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "tit",
        parent=styles["Normal"],
        fontName=font_b,
        fontSize=14,
        leading=18,
        alignment=TA_CENTER,
        spaceAfter=10,
    )
    meta_style = ParagraphStyle(
        "meta",
        parent=styles["Normal"],
        fontName=font_n,
        fontSize=9,
        leading=12,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#444444"),
    )
    foot_style = ParagraphStyle(
        "foot",
        parent=styles["Normal"],
        fontName=font_n,
        fontSize=7,
        leading=10,
        textColor=colors.HexColor("#666666"),
    )
    th_style = ParagraphStyle(
        "th",
        parent=styles["Normal"],
        fontName=font_b,
        fontSize=8,
        leading=10,
        alignment=TA_CENTER,
    )
    td_left = ParagraphStyle(
        "tdl",
        parent=styles["Normal"],
        fontName=font_n,
        fontSize=7,
        leading=9,
        alignment=TA_LEFT,
    )
    td_right = ParagraphStyle(
        "tdr",
        parent=styles["Normal"],
        fontName=font_n,
        fontSize=7,
        leading=9,
        alignment=TA_CENTER,
    )

    def _p(txt: str, style: ParagraphStyle) -> Paragraph:
        return Paragraph(escape(str(txt)), style)

    buf = BytesIO()
    left_m = 1.8 * cm
    right_m = 1.8 * cm
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=left_m,
        rightMargin=right_m,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )
    usable_w = A4[0] - left_m - right_m
    story: list[Any] = []

    story.append(Paragraph("Simulador de renda fixa", title_style))
    modo = "Real (IPCA)" if exibir_inflacao else "Nominal líquido"
    story.append(
        Paragraph(
            f"Prazo: <b>{prazo_meses}</b> meses · <b>{prazo_dias}</b> dias (IR) · {modo}<br/>"
            f"Capital: R$ {format_moeda_br(valor_total)}",
            meta_style,
        )
    )
    story.append(Spacer(1, 0.35 * cm))
    story.append(
        Paragraph(
            f"<b>Mercado (BCB)</b> — Selic meta {selic_meta_aa:.2f}% · CDI {cdi_aa:.2f}% · "
            f"IPCA 12m {ipca_12m:.2f}% · TR {tr_m:.4f}%",
            meta_style,
        )
    )
    story.append(Spacer(1, 0.45 * cm))

    headers = list(df.columns)
    ncols = len(headers)
    # Larguras proporcionais (Condição mais larga; Montante + Recebido ao final)
    fracs_7 = [0.11, 0.26, 0.07, 0.09, 0.09, 0.19, 0.19]
    if ncols == len(fracs_7):
        col_widths = [usable_w * f for f in fracs_7]
    else:
        col_widths = [usable_w / ncols] * ncols

    def _estilo_celula(nome_col: str) -> ParagraphStyle:
        if nome_col in ("Ativo", "Condição"):
            return td_left
        return td_right

    head_row = [_p(h, th_style) for h in headers]
    body_rows: list[list[Paragraph]] = []
    for row in df.values.tolist():
        cells = [_p(val, _estilo_celula(headers[j])) for j, val in enumerate(row)]
        body_rows.append(cells)

    tbl_data: list[list[Any]] = [head_row] + body_rows
    t = Table(tbl_data, repeatRows=1, colWidths=col_widths)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f8f8")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 0.5 * cm))
    story.append(
        Paragraph(
            "Fonte: séries SGS do Banco Central (API pública). Simulação educacional — "
            "não substitui assessoria; IR incide sobre rendimento; fundos podem ter come-cotas.",
            foot_style,
        )
    )

    doc.build(story)
    return buf.getvalue()


# --- Interface ---
st.set_page_config(
    page_title="Renda fixa — Hart Botelho CFP®",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
    /* Layout compacto sem “esmagar” a página: evite width="large" nas colunas da tabela — no Streamlit isso fixa ~400px e estoura a largura (barra horizontal + altura extra). */
    header[data-testid="stHeader"] {
        padding-top: 0.35rem !important;
        padding-bottom: 0.35rem !important;
    }
    div[data-testid="stToolbar"] { padding-top: 0 !important; padding-bottom: 0 !important; }
    footer { visibility: hidden !important; height: 0 !important; min-height: 0 !important; }
    section.main > div.block-container {
        padding-top: 0.65rem !important;
        padding-bottom: 0.15rem !important;
        max-width: 100%;
    }
    /* st.title() não herda bem text-align; título principal usa .hart-page-title-wrap */
    section.main h1 {
        font-size: 1.42rem !important;
        margin: 0 0 0.1rem 0 !important;
        line-height: 1.15 !important;
    }
    div[data-testid="stMarkdownContainer"]:has(.hart-page-title-wrap) {
        width: 100% !important;
    }
    .hart-page-title-wrap {
        width: 100% !important;
        max-width: 100% !important;
        margin: 0 0 0.12rem 0 !important;
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        box-sizing: border-box !important;
    }
    .hart-page-title-wrap .hart-page-title {
        font-size: 1.58rem !important;
        font-weight: 600 !important;
        line-height: 1.15 !important;
        margin: 0 !important;
        color: inherit !important;
    }
    section.main h2, section.main h3 {
        font-size: 1.02rem !important;
        margin: 0.05rem 0 0.2rem 0 !important;
        line-height: 1.2 !important;
    }
    hr {
        margin: 0.28rem 0 !important;
        border-color: #e8e8ef;
    }
    div[data-testid="stCaptionContainer"] p {
        margin-top: 0.1rem !important;
        margin-bottom: 0.15rem !important;
        font-size: 0.78rem !important;
        line-height: 1.25 !important;
    }
    section.main [data-testid="stNumberInput"] input { max-width: 7.5rem; }
    section.main [data-testid="stTextInput"] input { max-width: 11rem; }
    section.main [data-testid="stNumberInput"] label p { font-size: 0.8rem; }
    section.main div[data-testid="stRadio"] label { font-size: 0.78rem; }
    section.main [data-testid="stVerticalBlock"] > div {
        gap: 0.35rem !important;
    }
    div[data-testid="stMetricValue"] { font-size: 1.08rem !important; margin-top: 0 !important; }
    div[data-testid="stMetricLabel"] { font-size: 0.68rem !important; }
    div[data-testid="column"] [data-testid="stMetricValue"] { font-size: 1.08rem !important; }
    [data-testid="stDownloadButton"] button {
        padding-top: 0.3rem !important;
        padding-bottom: 0.3rem !important;
        min-height: 2.1rem !important;
    }
    .hart-credit-box {
        background: linear-gradient(135deg, #f0f4ff 0%, #e8eef8 100%);
        border: 1px solid #c5d0e6;
        border-radius: 8px;
        padding: 7px 12px;
        margin: 2px 0 6px 0;
        text-align: center;
        font-size: 0.98rem;
        color: #1a2744;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05);
    }
    .hart-credit-box .hart-name { font-weight: 700; font-size: 1.08rem; letter-spacing: 0.02em; }
    /* Colunas lado a lado: sem esticar a mais baixa (evita faixa branca enorme abaixo do PDF/resultados) */
    div[data-testid="stHorizontalBlock"] {
        align-items: flex-start !important;
    }
    div[data-testid="stDataFrame"] {
        font-size: 0.78rem;
        line-height: 1.2;
    }
    div[data-testid="stDataFrame"] [class*="glide-data-grid"] {
        font-size: 0.78rem !important;
    }
    div[data-testid="stDataFrame"] [class*="dvn"] {
        min-height: 22px !important;
    }
    div[data-testid="stAppViewContainer"] .main {
        overflow-x: hidden;
    }
    section.main p.hart-footer-note {
        font-size: 0.9rem !important;
        line-height: 1.45 !important;
        color: rgba(49, 51, 63, 0.72) !important;
        margin: 0.4rem 0 0 0 !important;
    }
    .hart-footer-note a {
        color: #1a4b8c;
        font-weight: 500;
        text-decoration: underline;
    }
    .hart-footer-note a:hover {
        color: #0d2d5c;
    }
    section.main p.hart-footer-note.hart-footer-simula-lance {
        margin: 0.55rem 0 0 0 !important;
        padding: 10px 14px !important;
        font-size: 0.95rem !important;
        font-weight: 600 !important;
        line-height: 1.45 !important;
        color: #1a2744 !important;
        background: linear-gradient(135deg, #f4f7ff 0%, #e8eef8 100%) !important;
        border: 1px solid #b8c5e0 !important;
        border-radius: 8px !important;
        box-shadow: 0 1px 3px rgba(26, 39, 68, 0.08) !important;
        text-align: center !important;
    }
    section.main p.hart-footer-note.hart-footer-simula-lance a {
        font-weight: 700 !important;
        color: #0d3d7a !important;
    }
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="hart-page-title-wrap"><span class="hart-page-title">Simulador de renda fixa</span></div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="hart-credit-box">Desenvolvido por <span class="hart-name">Hart Botelho</span>, CFP®</div>',
    unsafe_allow_html=True,
)
try:
    selic_meta_aa = consulta_bcb(SGS_SELIC_META_AA)
    cdi_dia = consulta_bcb(SGS_CDI_DIARIO)
    ipca_12m = consulta_bcb(SGS_IPCA_12M)
    tr_m = consulta_bcb(SGS_TR_MENSAL)
except Exception as e:
    st.error(f"Não foi possível obter dados do BCB: {e}")
    st.stop()

cdi_aa = cdi_percentual_anual(cdi_dia)
tx_mes_poup = poupança_taxa_mensal_aproximada(selic_meta_aa, tr_m, cdi_dia)
poupanca_aa_equiv = (1 + tx_mes_poup) ** 12 - 1

# Opções fixas para comparação com taxa negociada (CDB/LCI pré e pós).
OPCOES_NEGOCIACAO_RF = ["CDB · pós", "CDB · pré", "LCI/LCA · pós", "LCI/LCA · pré"]

col_param, col_result = st.columns([0.32, 0.68], gap="small")

with col_param:
    st.subheader("Parâmetros")
    g1, g2 = st.columns(2)
    with g1:
        if "capital_br_input" not in st.session_state:
            st.session_state.capital_br_input = format_moeda_br(50_000.0)
        capital_txt = st.text_input(
            "Capital (R$)",
            key="capital_br_input",
            help="Ponto nos milhares e vírgula nos centavos (ex.: 52.000,00).",
        )
        try:
            valor_total = max(0.0, parse_moeda_br(capital_txt))
        except ValueError:
            valor_total = 0.0
            st.caption("Capital inválido — use números, '.' e ','.")
    prazo_meses = int(g2.number_input("Prazo (meses)", min_value=1, max_value=600, value=12, step=1))

    st.divider()
    row_tit_nego, col_nego_chk = st.columns([0.58, 0.42])
    with row_tit_nego:
        st.markdown("**CDB** · **LCI/LCA**")
    with col_nego_chk:
        exibir_negociacao = st.checkbox(
            "Condições negociáveis",
            value=False,
            key="chk_cond_nego",
            help="Ao marcar, abre o bloco de taxa negociada e carência logo abaixo (antes do Fundo DI).",
        )

    col_cdb, col_lci = st.columns(2)

    with col_cdb:
        inc_cdb = st.checkbox("CDB", value=True, key="inc_cdb")
        inc_cdb_pos = st.checkbox("Incluir pós (% CDI)", value=True, key="inc_cdb_pos", disabled=not inc_cdb)
        inc_cdb_pre = st.checkbox("Incluir pré (% a.a.)", value=False, key="inc_cdb_pre", disabled=not inc_cdb)
        pct_cdb = st.number_input(
            "% CDI (pós)",
            min_value=0.0,
            max_value=300.0,
            value=100.0,
            step=0.5,
            format="%.2f",
            key="pct_cdb",
            disabled=not inc_cdb or not inc_cdb_pos,
        )
        pre_cdb = st.number_input(
            "Pré a.a. % (pré)",
            min_value=0.0,
            max_value=100.0,
            value=12.0,
            step=0.1,
            format="%.2f",
            key="pre_cdb",
            disabled=not inc_cdb or not inc_cdb_pre,
        )

    with col_lci:
        inc_lci = st.checkbox("LCI/LCA", value=True, key="inc_lci")
        inc_lci_pos = st.checkbox("Incluir pós (% CDI) ", value=True, key="inc_lci_pos", disabled=not inc_lci)
        inc_lci_pre = st.checkbox("Incluir pré (% a.a.) ", value=False, key="inc_lci_pre", disabled=not inc_lci)
        pct_lci = st.number_input(
            "% CDI (pós) ",
            min_value=0.0,
            max_value=300.0,
            value=90.0,
            step=0.5,
            format="%.2f",
            key="pct_lci",
            disabled=not inc_lci or not inc_lci_pos,
        )
        pre_lci = st.number_input(
            "Pré a.a. % (pré) ",
            min_value=0.0,
            max_value=100.0,
            value=11.5,
            step=0.1,
            format="%.2f",
            key="pre_lci",
            disabled=not inc_lci or not inc_lci_pre,
        )

    if exibir_negociacao:
        st.markdown("##### Condições Negociais — taxa negociada e carência")
        st.caption(
            "A linha ✨ na tabela aparece logo abaixo do ativo correspondente (se ele estiver no quadro). "
            "CDB e LCI: em pós informe % do CDI; em pré informe a taxa % a.a."
        )
        ativo_negociado = st.selectbox(
            "Ativo a negociar",
            options=OPCOES_NEGOCIACAO_RF,
            key="nego_ativo",
        )
        _nego_e_pre = "pré" in ativo_negociado
        _label_taxa_nego = (
            "Taxa negociada (% do CDI)"
            if not _nego_e_pre
            else "Taxa negociada (% a.a.)"
        )
        _help_taxa_nego = (
            "Percentual do CDI negociado (pós-fixado), para CDB ou LCI/LCA."
            if not _nego_e_pre
            else "Taxa pré-fixada anual negociada (% a.a.), para CDB ou LCI/LCA."
        )
        nova_taxa_negociada = st.number_input(
            _label_taxa_nego,
            min_value=0.0,
            max_value=500.0,
            value=0.0,
            step=0.05,
            format="%.2f",
            key="nego_taxa",
            help=_help_taxa_nego,
        )
        meses_carencia = st.number_input(
            "Prazo de Carência (meses)",
            min_value=0,
            max_value=600,
            value=6,
            step=1,
            key="nego_carencia",
        )

    st.divider()
    st.markdown("**Fundo DI**")
    inc_fundo = st.checkbox("Ativo", value=False, key="inc_fundo")
    f_cd, f_adm = st.columns(2)
    with f_cd:
        pct_fundo_cdi = st.number_input(
            "% CDI",
            min_value=0.0,
            max_value=300.0,
            value=100.0,
            step=0.5,
            format="%.2f",
            key="pct_fundo_cdi",
            disabled=not inc_fundo,
        )
    with f_adm:
        adm_fundo = st.number_input(
            "Adm % a.a.",
            min_value=0.0,
            max_value=10.0,
            value=0.5,
            step=0.05,
            format="%.2f",
            key="adm_fundo",
            disabled=not inc_fundo,
        )

with col_result:
    st.markdown("**Mercado (BCB)** · SGS / API pública")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Selic meta", f"{selic_meta_aa:.2f}%", help="Série 432, % a.a.")
    m2.metric("CDI (impl.)", f"{cdi_aa:.2f}%", help="A partir do CDI diário (série 12).")
    m3.metric("IPCA 12m", f"{ipca_12m:.2f}%", help="Acumulado 12 meses (série 13522).")
    m4.metric("TR mês", f"{tr_m:.4f}%", help="Série 7811.")

    row_res_t, row_res_g, row_res_p = st.columns([1.35, 1.25, 1.4])
    with row_res_t:
        st.markdown("**Resultados**")
    with row_res_g:
        exibir_inflacao = st.checkbox(
            "Ganho real (IPCA)",
            value=False,
            help="Quando marcado, desconta o IPCA acumulado em 12 meses (última leitura BCB).",
        )
    with row_res_p:
        inc_poup = st.checkbox(
            "Incluir Poupança no quadro",
            value=True,
            key="inc_poup",
            help="Quando desmarcado, a Poupança não aparece na tabela de resultados.",
        )

exibir_negociacao_ss = bool(st.session_state.get("chk_cond_nego", False))
ativo_negociado = str(st.session_state.get("nego_ativo", OPCOES_NEGOCIACAO_RF[0]))
nova_taxa_negociada = float(st.session_state.get("nego_taxa", 0.0))
meses_carencia = int(st.session_state.get("nego_carencia", 6))

linhas = montar_linhas(
    inc_poup=inc_poup,
    inc_cdb=inc_cdb,
    inc_cdb_pos=inc_cdb_pos,
    inc_cdb_pre=inc_cdb_pre,
    pct_cdb=pct_cdb,
    pre_cdb=pre_cdb,
    inc_lci=inc_lci,
    inc_lci_pos=inc_lci_pos,
    inc_lci_pre=inc_lci_pre,
    pct_lci=pct_lci,
    pre_lci=pre_lci,
    inc_fundo=inc_fundo,
    pct_fundo_cdi=pct_fundo_cdi,
    adm_fundo=adm_fundo,
    poupanca_aa_equiv=poupanca_aa_equiv,
    cdi_aa=cdi_aa,
)

negociacao_ativa = exibir_negociacao_ss and nova_taxa_negociada > 0

with col_result:
    prazo_dias = int(prazo_meses * 30)

    resultados: list[dict[str, str]] = []
    nego_linha_inserida = False

    for info in linhas:
        resultados.append(
            resultado_para_tabela(
                info,
                valor_total=valor_total,
                prazo_meses=prazo_meses,
                prazo_dias=prazo_dias,
                exibir_inflacao=exibir_inflacao,
                ipca_12m=ipca_12m,
            )
        )
        if (
            negociacao_ativa
            and info["nome"] == ativo_negociado
            and "taxa_bruta_anual" in info
        ):
            info_neg = linha_negociada_de_pai(
                info,
                nova_taxa_negociada,
                meses_carencia,
                cdi_aa,
            )
            resultados.append(
                resultado_para_tabela(
                    info_neg,
                    valor_total=valor_total,
                    prazo_meses=prazo_meses,
                    prazo_dias=prazo_dias,
                    exibir_inflacao=exibir_inflacao,
                    ipca_12m=ipca_12m,
                )
            )
            nego_linha_inserida = True

    # Pré (ou pós) negociado sem a linha correspondente na prateleira: antes não aparecia no quadro.
    if negociacao_ativa and not nego_linha_inserida:
        pai_sint = info_pai_negociacao(ativo_negociado)
        if pai_sint is not None:
            info_neg = linha_negociada_de_pai(
                pai_sint,
                nova_taxa_negociada,
                meses_carencia,
                cdi_aa,
            )
            resultados.append(
                resultado_para_tabela(
                    info_neg,
                    valor_total=valor_total,
                    prazo_meses=prazo_meses,
                    prazo_dias=prazo_dias,
                    exibir_inflacao=exibir_inflacao,
                    ipca_12m=ipca_12m,
                )
            )

    if not resultados:
        st.warning("Marque ao menos um produto à esquerda (e modalidades CDB/LCI, se aplicável).")
    else:
        _modo = "real (IPCA)" if exibir_inflacao else "nominal líq."
        _det = (
            f"Efetivas após IPCA ~{ipca_12m:.1f}% e IR."
            if exibir_inflacao
            else "Nominais líquidas de IR."
        )
        st.caption(f"{prazo_meses} meses · {prazo_dias} dias (IR) · {_modo}. {_det}")

        df_res = pd.DataFrame(resultados)
        _n = len(resultados)
        # Glide usa ~38–42px/linha; 22px subestimava → barra de rolagem dentro da tabela.
        _h = 52 + _n * 42
        st.dataframe(
            df_res,
            use_container_width=True,
            hide_index=True,
            height=min(520, max(110, _h)),
            column_config={
                # Streamlit: small≈75px, medium≈200px, large≈400px — NÃO use "large" na coluna Ativo (estoura a tabela).
                "Ativo": st.column_config.TextColumn("Ativo", width="medium"),
                "Condição": st.column_config.TextColumn("Condição", width="medium"),
                "IR": st.column_config.TextColumn("IR", width="small"),
                "% mês": st.column_config.TextColumn("% mês", width="small"),
                "% a.a.": st.column_config.TextColumn("% a.a.", width="small"),
                # Valores R$ precisam de mais que "small" (~75px) para não cortar centavos
                "Montante": st.column_config.TextColumn("Montante", width=None),
                "Recebido": st.column_config.TextColumn("Recebido", width=None),
            },
        )
        try:
            pdf_bytes = gerar_pdf_resultados(
                df_res,
                prazo_meses=prazo_meses,
                prazo_dias=prazo_dias,
                valor_total=valor_total,
                exibir_inflacao=exibir_inflacao,
                selic_meta_aa=selic_meta_aa,
                cdi_aa=cdi_aa,
                ipca_12m=ipca_12m,
                tr_m=tr_m,
            )
            st.download_button(
                label="Baixar PDF",
                data=pdf_bytes,
                file_name="simulador_renda_fixa_resultados.pdf",
                mime="application/pdf",
                help="Relatório em PDF com os mesmos itens da tabela.",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"Não foi possível gerar o PDF ({e}). Instale: pip install reportlab")

st.markdown(
    '<p class="hart-footer-note">Fonte: SGS/BCB · educacional — não substitui assessoria; IR sobre rendimento; fundos podem ter come-cotas.</p>'
    '<p class="hart-footer-note hart-footer-simula-lance">Conheça também o '
    '<a href="https://simula-lance.streamlit.app/" target="_blank" rel="noopener noreferrer">Simula Lance</a> '
    '— simulador de consórcio.</p>',
    unsafe_allow_html=True,
)
