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
    section.main h1 {
        font-size: 1.42rem !important;
        margin: 0 0 0.1rem 0 !important;
        line-height: 1.15 !important;
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
</style>
""",
    unsafe_allow_html=True,
)

st.title("Simulador de renda fixa")
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
    st.markdown("**CDB** · **LCI/LCA**")
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

    st.divider()
    st.markdown("**Fundo DI**")
    f1, f2, f3 = st.columns([0.9, 1, 1])
    inc_fundo = f1.checkbox("Ativo", value=False, key="inc_fundo")
    pct_fundo_cdi = f2.number_input(
        "% CDI",
        min_value=0.0,
        max_value=300.0,
        value=100.0,
        step=0.5,
        format="%.2f",
        key="pct_fundo_cdi",
        disabled=not inc_fundo,
    )
    adm_fundo = f3.number_input(
        "Adm % a.a.",
        min_value=0.0,
        max_value=10.0,
        value=0.5,
        step=0.05,
        format="%.2f",
        key="adm_fundo",
        disabled=not inc_fundo,
    )

    st.divider()
    inc_poup = st.checkbox("Poupança (regra BCB Selic/TR)", value=True, key="inc_poup")

with col_result:
    st.markdown("**Mercado (BCB)** · SGS / API pública")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Selic meta", f"{selic_meta_aa:.2f}%", help="Série 432, % a.a.")
    m2.metric("CDI (impl.)", f"{cdi_aa:.2f}%", help="A partir do CDI diário (série 12).")
    m3.metric("IPCA 12m", f"{ipca_12m:.2f}%", help="Acumulado 12 meses (série 13522).")
    m4.metric("TR mês", f"{tr_m:.4f}%", help="Série 7811.")

    row_res_t, row_res_g = st.columns([2.0, 1.1])
    with row_res_t:
        st.markdown("**Resultados**")
    with row_res_g:
        exibir_inflacao = st.checkbox(
            "Ganho real (IPCA)",
            value=False,
            help="Quando marcado, desconta o IPCA acumulado em 12 meses (última leitura BCB).",
        )

    prazo_dias = int(prazo_meses * 30)

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
                "taxa_bruta_anual": bruta_fundo,
                "detalhe": f"{pct_fundo_cdi:.2f}% CDI − {adm_fundo:.2f}% adm",
            }
        )

    resultados: list[dict[str, str]] = []

    for info in linhas:
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

        resultados.append(
            {
                "Ativo": str(info["nome"]),
                "Condição": str(info.get("detalhe", "")),
                "IR": ir_label,
                "% mês": f"{taxa_mensal * 100:.3f}",
                "% a.a.": f"{taxa_exibida * 100:.2f}",
                "Montante": f"R$ {format_moeda_br(valor_final)}",
                "Recebido": f"R$ {format_moeda_br(recebido)}",
            }
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

st.caption(
    "Fonte: SGS/BCB · educacional — não substitui assessoria; IR sobre rendimento; fundos podem ter come-cotas. "
    "Conheça também o [Simula Lance](https://simula-lance.streamlit.app/) — simulador de consórcio."
)
