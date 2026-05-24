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


def prazo_dias_corridos(prazo_meses: int) -> int:
    """dias = t × 30 (parâmetro da tabela regressiva de IR)."""
    return int(prazo_meses) * 30


def taxa_mensal_de_anual(taxa_aa_decimal: float) -> float:
    """(1 + taxa_aa)^(1/12) − 1 — equivalência composta anual → mensal."""
    return (1.0 + taxa_aa_decimal) ** (1.0 / 12.0) - 1.0


def cdi_am(cdi_aa_decimal: float) -> float:
    """CDI_am = (1 + CDI_aa)^(1/12) − 1. CDI_aa em decimal (ex.: 0,144)."""
    return taxa_mensal_de_anual(cdi_aa_decimal)


def cdi_taxa_mensal(cdi_aa_percent: float) -> float:
    """Atalho quando CDI está em % a.a. (ex.: 14,40)."""
    return cdi_am(cdi_aa_percent / 100.0)


def ativo_isento_ir(tipo: str) -> bool:
    """LCI, LCA e Poupança: isento == true → α = 0."""
    return tipo in ("Poupança", "LCI/LCA")


def aliquota_ir_regressiva(dias: int, *, isento: bool = False) -> float:
    """α na tabela regressiva (dias corridos)."""
    if isento:
        return 0.0
    if dias <= 180:
        return 0.225
    if dias <= 360:
        return 0.20
    if dias <= 720:
        return 0.175
    return 0.15


def calcular_ir(prazo_dias: int, tipo_ativo: str) -> float:
    """Compatibilidade: delega para aliquota_ir_regressiva + isento por tipo."""
    return aliquota_ir_regressiva(prazo_dias, isento=ativo_isento_ir(tipo_ativo))


def tipo_ir_de_rotulo(rotulo: str) -> str:
    if rotulo == "Poupança":
        return "Poupança"
    if "LCI" in rotulo or "LCA" in rotulo:
        return "LCI/LCA"
    return "CDB"


def taxa_aa_bruta_composta_pos(cdi_aa_percent: float, pct_cdi: float) -> float:
    """Taxa anual bruta equivalente (composta) para exibição em pós-fixado % CDI."""
    i_mes = cdi_taxa_mensal(cdi_aa_percent) * (pct_cdi / 100.0)
    return (1.0 + i_mes) ** 12 - 1.0


def i_mes_efetivo(
    *,
    modalidade: str,
    tipo: str,
    taxa_input: float,
    cdi_aa_percent: float,
    poupanca_taxa_mensal: float,
    adm_fundo_aa_percent: float = 0.5,
) -> float:
    """
    Taxa mensal i_mes (decimal) que compõe M_bruto = C×(1+i_mes)^t.
    Pós: CDI_am×(%CDI/100). Pré: (1+taxa_pré_aa)^(1/12)−1. Fundo: CDI_am×%CDI−A_mes. Poupança: regra regulatória.
    """
    if tipo == "Poupança":
        return max(0.0, float(poupanca_taxa_mensal))
    if modalidade == "pre":
        return taxa_mensal_de_anual(taxa_input / 100.0)
    return max(cdi_taxa_mensal(cdi_aa_percent) * (taxa_input / 100.0), 0.0)


def i_mes_fundo_di(
    pct_cdi_percent: float,
    cdi_aa_percent: float,
    adm_fundo_aa_percent: float,
) -> float:
    """i_mes = CDI_am×%CDI − A_mes."""
    a_mes = taxa_mensal_de_anual(adm_fundo_aa_percent / 100.0)
    return max(cdi_taxa_mensal(cdi_aa_percent) * (pct_cdi_percent / 100.0) - a_mes, 0.0)


def i_mes_de_info(
    info: dict[str, Any],
    *,
    cdi_aa_percent: float,
    poupanca_taxa_mensal: float,
) -> float:
    """Resolve i_mes a partir do metadado do ativo (comparativo e carteira)."""
    nome = str(info.get("nome", ""))
    tipo = str(info.get("tipo", "CDB"))
    modalidade = str(info.get("modalidade", "pos"))
    if tipo == "Poupança" or nome.startswith("Poupança"):
        return max(0.0, float(poupanca_taxa_mensal))
    if nome == "Fundo DI" or info.get("produto") == "fundo_di":
        return i_mes_fundo_di(
            float(info.get("taxa_input", 100.0)),
            cdi_aa_percent,
            float(info.get("adm_fundo") or 0.5),
        )
    taxa_input = float(info.get("taxa_input", 100.0))
    return i_mes_efetivo(
        modalidade=modalidade,
        tipo=tipo,
        taxa_input=taxa_input,
        cdi_aa_percent=cdi_aa_percent,
        poupanca_taxa_mensal=poupanca_taxa_mensal,
    )


def isento_de_info(info: dict[str, Any]) -> bool:
    if "isento" in info:
        return bool(info["isento"])
    return ativo_isento_ir(str(info.get("tipo", "CDB")))


def simular_ativo_renda_fixa(
    capital: float,
    prazo_meses: int,
    *,
    i_mes: float,
    isento: bool = False,
    prazo_dias: int | None = None,
) -> dict[str, float]:
    """
    Fluxo: i_mes → M_bruto → L_bruto → IR → M_final.
    IR = L_bruto×α; Recebido = L_bruto−IR; M_final = C+Recebido.
    Taxas de exibição: i_mes_liq = (M_final/C)^(1/t)−1; i_ano_liq = (1+i_mes_liq)^12−1.
    """
    capital = max(0.0, float(capital))
    i_mes = max(0.0, float(i_mes))
    t = max(0, int(prazo_meses))
    dias = int(prazo_dias) if prazo_dias is not None else prazo_dias_corridos(t)

    montante_bruto = capital * (1.0 + i_mes) ** t
    lucro_bruto = montante_bruto - capital
    aliquota_ir = aliquota_ir_regressiva(dias, isento=isento)
    imposto = lucro_bruto * aliquota_ir
    recebido = lucro_bruto - imposto
    montante_liquido = capital + recebido

    if capital > 0 and t > 0:
        taxa_mensal_liq = (montante_liquido / capital) ** (1.0 / t) - 1.0
        taxa_aa_liq = (1.0 + taxa_mensal_liq) ** 12 - 1.0
        taxa_aa_bruta = (1.0 + i_mes) ** 12 - 1.0
    else:
        taxa_mensal_liq = 0.0
        taxa_aa_liq = 0.0
        taxa_aa_bruta = 0.0

    return {
        "montante_bruto": montante_bruto,
        "lucro_bruto": lucro_bruto,
        "aliquota_ir": aliquota_ir,
        "imposto": imposto,
        "recebido": recebido,
        "montante_liquido": montante_liquido,
        "taxa_mensal_bruta": i_mes,
        "taxa_mensal_liq": taxa_mensal_liq,
        "taxa_aa_bruta": taxa_aa_bruta,
        "taxa_aa_liq": taxa_aa_liq,
    }


def projetar_ativo_renda_fixa(
    capital: float,
    taxa_mensal_bruta: float,
    prazo_meses: int,
    prazo_dias: int,
    tipo_ir: str,
) -> dict[str, float]:
    """Compatibilidade (carteira): delega para simular_ativo_renda_fixa."""
    return simular_ativo_renda_fixa(
        capital,
        prazo_meses,
        i_mes=taxa_mensal_bruta,
        isento=ativo_isento_ir(tipo_ir),
        prazo_dias=prazo_dias,
    )


def taxa_mensal_bruta_ativo(
    rotulo: str,
    taxa_input: float,
    *,
    cdi_aa_percent: float,
    poupanca_taxa_mensal: float,
    adm_fundo_aa_percent: float = 0.5,
) -> float:
    """Atalho por rótulo (modo carteira)."""
    tipo = tipo_ir_de_rotulo(rotulo)
    if rotulo == "Fundo DI":
        return i_mes_fundo_di(taxa_input, cdi_aa_percent, adm_fundo_aa_percent)
    modalidade = "pre" if "pré" in rotulo else "pos"
    return i_mes_efetivo(
        modalidade=modalidade,
        tipo=tipo,
        taxa_input=taxa_input,
        cdi_aa_percent=cdi_aa_percent,
        poupanca_taxa_mensal=poupanca_taxa_mensal,
    )


def projetar_montante(
    capital: float,
    taxa_efetiva_anual: float,
    meses: int,
    compounding_mensal: bool = True,
) -> tuple[float, float]:
    """Projeção por taxa anual (referência CDI 100% e compat. legado)."""
    r_a = taxa_efetiva_anual
    r_m = taxa_mensal_de_anual(r_a) if compounding_mensal else r_a / 12.0
    final = capital * (1.0 + r_m) ** meses
    return final, r_m


def taxa_bruta_anual_decimal(
    modalidade: str,
    pct_do_cdi: float,
    taxa_pre_fixada_aa: float,
    cdi_aa_percent: float,
) -> float:
    """Taxa anual bruta em decimal — composta para pós; linear para pré (exibição legada)."""
    if modalidade == "pos":
        return taxa_aa_bruta_composta_pos(cdi_aa_percent, pct_do_cdi)
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
                "modalidade": "poupanca",
                "isento": True,
                "taxa_liquida_anual": poupanca_aa_equiv,
                "taxa_input": 0.0,
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
                "isento": False,
                "taxa_bruta_anual": bruta,
                "taxa_input": pct_cdb,
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
                "isento": False,
                "taxa_bruta_anual": bruta,
                "taxa_input": pre_cdb,
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
                "isento": True,
                "taxa_bruta_anual": bruta,
                "taxa_input": pct_lci,
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
                "isento": True,
                "taxa_bruta_anual": bruta,
                "taxa_input": pre_lci,
                "detalhe": f"Pré {pre_lci:.2f}% a.a.",
            }
        )

    if inc_fundo:
        i_fundo = i_mes_fundo_di(pct_fundo_cdi, cdi_aa, adm_fundo)
        bruta_fundo = (1.0 + i_fundo) ** 12 - 1.0
        linhas.append(
            {
                "nome": "Fundo DI",
                "tipo": "CDB",
                "modalidade": "pos",
                "produto": "fundo_di",
                "isento": False,
                "taxa_bruta_anual": bruta_fundo,
                "taxa_input": pct_fundo_cdi,
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
        "isento": ativo_isento_ir(str(info_pai["tipo"])),
        "taxa_bruta_anual": bruta,
        "taxa_input": nova_taxa_percent,
        "detalhe": detalhe,
        "condicao_negocial": True,
    }


def _i_mes_simulacao_comparativo(
    info: dict[str, Any],
    *,
    cdi_aa_percent: float,
    poupanca_taxa_mensal: float,
) -> float:
    """i_mes para simulação no comparativo; LCI/LCA pós = CDI_am × %CDI (sem atalhos anuais)."""
    tipo = str(info.get("tipo", "CDB"))
    modalidade = str(info.get("modalidade", "pos"))
    if tipo == "LCI/LCA" and modalidade == "pos":
        pct_cdi = float(info.get("taxa_input", 0.0)) / 100.0
        return cdi_taxa_mensal(cdi_aa_percent) * pct_cdi
    return i_mes_de_info(
        info,
        cdi_aa_percent=cdi_aa_percent,
        poupanca_taxa_mensal=poupanca_taxa_mensal,
    )


def resultado_para_tabela(
    info: dict[str, Any],
    *,
    valor_total: float,
    prazo_meses: int,
    prazo_dias: int,
    exibir_inflacao: bool,
    ipca_12m: float,
    cdi_aa_percent: float = 0.0,
    poupanca_taxa_mensal: float = 0.0,
    comparativo: bool = False,
) -> dict[str, str]:
    """Uma linha da tabela: Taxa → M_bruto → L_bruto → IR → M_líquido."""
    if comparativo:
        i_mes = _i_mes_simulacao_comparativo(
            info,
            cdi_aa_percent=cdi_aa_percent,
            poupanca_taxa_mensal=poupanca_taxa_mensal,
        )
    else:
        i_mes = i_mes_de_info(
            info,
            cdi_aa_percent=cdi_aa_percent,
            poupanca_taxa_mensal=poupanca_taxa_mensal,
        )
    isento = isento_de_info(info)
    aliquota_ir = aliquota_ir_regressiva(prazo_dias, isento=isento)

    sim = simular_ativo_renda_fixa(
        valor_total,
        prazo_meses,
        i_mes=i_mes,
        isento=isento,
        prazo_dias=prazo_dias,
    )

    recebido = sim["lucro_bruto"] - sim["imposto"]
    valor_final = valor_total + recebido

    # % mês e % a.a. sempre líquidos (após IR quando houver)
    taxa_mensal = sim["taxa_mensal_liq"]
    taxa_exibida = sim["taxa_aa_liq"]

    if exibir_inflacao:
        ipca_decimal = ipca_12m / 100.0
        montante_liquido = valor_final
        rent_liq_periodo = (
            (montante_liquido / valor_total - 1.0) if valor_total > 0 else 0.0
        )
        ipca_periodo = (
            (1.0 + ipca_decimal) ** (prazo_meses / 12.0) - 1.0
            if prazo_meses > 0
            else 0.0
        )
        rent_real_periodo = (1.0 + rent_liq_periodo) / (1.0 + ipca_periodo) - 1.0
        valor_final = valor_total * (1.0 + rent_real_periodo)
        recebido = valor_final - valor_total
        if prazo_meses > 0:
            taxa_mensal = (1.0 + rent_real_periodo) ** (1.0 / prazo_meses) - 1.0
            taxa_exibida = (1.0 + rent_real_periodo) ** (12.0 / prazo_meses) - 1.0
        else:
            taxa_mensal = 0.0
            taxa_exibida = 0.0

    ir_label = "0%" if aliquota_ir <= 0 else f"{aliquota_ir * 100:.1f}%"

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


ROTULOS_CARTEIRA = [
    "CDB · pós",
    "CDB · pré",
    "LCI/LCA · pós",
    "LCI/LCA · pré",
    "Poupança",
    "Fundo DI",
]

_TXT_RENTAB_EQUIV = "Rent Equivalente"
_TXT_CDI_LIQ = "Equivale a % do CDI líquido"
_LABEL_CDI_LIQ_AB_METRIC = "A · B — Equivale a\n% do CDI líquido"


def posicao_para_info(
    rotulo: str,
    taxa: float,
    *,
    cdi_aa: float,
    poupanca_aa_equiv: float,
    adm_fundo: float = 0.5,
) -> dict[str, Any]:
    """Converte uma posição da carteira no mesmo formato usado pelo comparativo."""
    if rotulo == "Poupança":
        return {
            "nome": rotulo,
            "tipo": "Poupança",
            "modalidade": "poupanca",
            "isento": True,
            "taxa_liquida_anual": poupanca_aa_equiv,
            "taxa_input": 0.0,
            "detalhe": f"~{poupanca_aa_equiv * 100:.2f}% a.a. eq.",
        }
    if rotulo == "Fundo DI":
        i_f = i_mes_fundo_di(taxa, cdi_aa, adm_fundo)
        bruta_fundo = (1.0 + i_f) ** 12 - 1.0
        return {
            "nome": "Fundo DI",
            "tipo": "CDB",
            "modalidade": "pos",
            "produto": "fundo_di",
            "isento": False,
            "taxa_bruta_anual": bruta_fundo,
            "taxa_input": taxa,
            "adm_fundo": adm_fundo,
            "detalhe": f"{taxa:.2f}% CDI − {adm_fundo:.2f}% adm",
        }
    pai = info_pai_negociacao(rotulo)
    if pai is None:
        raise ValueError(f"Tipo de ativo desconhecido: {rotulo}")
    modalidade = str(pai.get("modalidade") or "pos")
    tipo = str(pai["tipo"])
    if modalidade == "pre":
        bruta = taxa_bruta_anual_decimal("pre", 0.0, taxa, cdi_aa)
        detalhe = f"Pré {taxa:.2f}% a.a."
    else:
        bruta = taxa_bruta_anual_decimal("pos", taxa, 0.0, cdi_aa)
        detalhe = f"{taxa:.2f}% CDI → ~{bruta * 100:.2f}% {_TXT_RENTAB_EQUIV}"
    return {
        "nome": rotulo,
        "tipo": tipo,
        "modalidade": modalidade,
        "isento": ativo_isento_ir(tipo),
        "taxa_bruta_anual": bruta,
        "taxa_input": taxa,
        "detalhe": detalhe,
    }


OPCOES_IR_CARTEIRA = [22.5, 20.0, 17.5, 15.0]


def carteira_tem_ir(rotulo: str) -> bool:
    return rotulo in ("CDB · pós", "CDB · pré", "Fundo DI")


def _fmt_ir_opcao(pct: float) -> str:
    return f"{pct:.1f}".replace(".", ",") + "%"


def normalizar_ir_carteira_pct(valor: float) -> float:
    return min(OPCOES_IR_CARTEIRA, key=lambda x: abs(x - float(valor)))


def ir_padrao_carteira_pct(prazo_dias: int) -> float:
    alvo = calcular_ir(prazo_dias, "CDB") * 100.0
    return normalizar_ir_carteira_pct(alvo)


def garantir_ir_posicoes(posicoes: list[dict[str, Any]], prazo_dias: int) -> None:
    padrao = ir_padrao_carteira_pct(prazo_dias)
    for pos in posicoes:
        if carteira_tem_ir(str(pos.get("rotulo", ""))):
            if pos.get("aliquota_ir") is None:
                pos["aliquota_ir"] = padrao
            else:
                pos["aliquota_ir"] = normalizar_ir_carteira_pct(float(pos["aliquota_ir"]))


def sincronizar_ir_widgets_para_posicoes(posicoes: list[dict[str, Any]]) -> None:
    for pos in posicoes:
        if not carteira_tem_ir(str(pos.get("rotulo", ""))):
            continue
        chave = f"cart_ir_sel_{int(pos['id'])}"
        if chave in st.session_state:
            pos["aliquota_ir"] = normalizar_ir_carteira_pct(float(st.session_state[chave]))


def _sync_ir_carteira(pid: int) -> None:
    chave = f"cart_ir_sel_{pid}"
    if chave not in st.session_state:
        return
    ir_val = float(st.session_state[chave])
    for pos in st.session_state.carteira_posicoes:
        if int(pos["id"]) == pid:
            pos["aliquota_ir"] = ir_val
            break


def _fmt_pct_input(valor: float) -> str:
    return f"{valor:.0f}" if abs(valor - round(valor)) < 1e-6 else f"{valor:.2f}"


def _fmt_condicao_rent_equiv(capital: float, taxa_aa_pct: float) -> str:
    """Ex.: R$ 30.000,00 11,58% Rent Equivalente"""
    return f"R$ {format_moeda_br(capital)} {taxa_aa_pct:.2f}% {_TXT_RENTAB_EQUIV}"


def condicao_posicao_carteira(
    rotulo: str,
    taxa: float,
    capital: float,
    *,
    cdi_aa: float,
    adm_fundo: float = 0.5,
    poupanca_aa_equiv: float = 0.0,
) -> str:
    """Condição do ativo: capital + rentabilidade equivalente anual (antes do IR)."""
    if rotulo == "Poupança":
        return f"R$ {format_moeda_br(capital)} ~{poupanca_aa_equiv * 100:.2f}% a.a. eq."
    if rotulo == "Fundo DI":
        i_f = i_mes_fundo_di(taxa, cdi_aa, adm_fundo)
        bruta_pct = ((1.0 + i_f) ** 12 - 1.0) * 100.0
        return _fmt_condicao_rent_equiv(capital, bruta_pct)
    if "pré" in rotulo:
        return f"R$ {format_moeda_br(capital)} Pré {taxa:.2f}% a.a."
    bruta_pct = taxa_bruta_anual_decimal("pos", taxa, 0.0, cdi_aa) * 100.0
    return _fmt_condicao_rent_equiv(capital, bruta_pct)


def condicao_total_carteira(
    posicoes: list[dict[str, Any]],
    *,
    capital_alocado: float,
    rendimento_bruto: float,
    prazo_meses: int,
    cdi_aa: float,
    poupanca_aa_equiv: float,
) -> str:
    """Resumo da carteira: replica a posição única ou capital + taxa bruta consolidada."""
    ativos = [p for p in posicoes if max(0.0, float(p.get("valor") or 0.0)) > 0]
    if len(ativos) == 1:
        p = ativos[0]
        return condicao_posicao_carteira(
            str(p["rotulo"]),
            float(p.get("taxa") or 0.0),
            float(p["valor"]),
            cdi_aa=cdi_aa,
            adm_fundo=float(p.get("adm_fundo") or 0.5),
            poupanca_aa_equiv=poupanca_aa_equiv,
        )
    n = max(0, int(prazo_meses))
    if capital_alocado > 0 and n > 0:
        taxa_bruta = (
            (capital_alocado + rendimento_bruto) / capital_alocado
        ) ** (12.0 / n) - 1.0
        return _fmt_condicao_rent_equiv(capital_alocado, taxa_bruta * 100.0)
    return f"R$ {format_moeda_br(capital_alocado)}"


CARTEIRAS_IDS = ("A", "B")


_NOMES_CARTEIRA = {"A": "Carteira A", "B": "Carteira B"}

_COL_IR_EFETIVO = "IR Efetivo"
_LABEL_IR_EFETIVO = "IR\nEfetivo"
_COLS_CARTEIRA_UI = ["Ativo", "Condição", _COL_IR_EFETIVO, "% mês", "% a.a.", "Montante", "Recebido"]


def _titulo_col_carteira(nome: str) -> str:
    """Rótulo de coluna na UI (quebra IR / Efetivo em duas linhas)."""
    if nome == _COL_IR_EFETIVO:
        return "IR<br/>Efetivo"
    return nome


def _titulo_col_carteira_pdf(nome: str) -> str:
    if nome == _COL_IR_EFETIVO:
        return "IR<br/>Efetivo"
    return nome


def formatar_ir_efetivo(ir_pago: float, lucro_bruto: float) -> str:
    """IR efetivo = IR pago ÷ lucro bruto (rendimento antes do IR)."""
    if lucro_bruto <= 0:
        return "0%"
    return f"{ir_pago / lucro_bruto * 100.0:.1f}%"


def _carteira_estado_vazio(carteira_id: str) -> dict[str, Any]:
    return {"nome": _NOMES_CARTEIRA[carteira_id], "posicoes": [], "next_id": 1}


def inicializar_estado_carteiras() -> None:
    """Duas carteiras (A e B); migra legado carteira_posicoes → Carteira A."""
    if "carteiras" not in st.session_state:
        legado = st.session_state.pop("carteira_posicoes", [])
        legado_nid = int(st.session_state.pop("carteira_next_id", 1))
        st.session_state.carteiras = {
            "A": _carteira_estado_vazio("A"),
            "B": _carteira_estado_vazio("B"),
        }
        if legado:
            st.session_state.carteiras["A"]["posicoes"] = legado
            st.session_state.carteiras["A"]["next_id"] = max(legado_nid, 1)


def _carteira_por_id(carteira_id: str) -> dict[str, Any]:
    inicializar_estado_carteiras()
    return st.session_state.carteiras[carteira_id]


def _excluir_posicao_carteira(carteira_id: str, pid: int) -> None:
    cart = _carteira_por_id(carteira_id)
    cart["posicoes"] = [p for p in cart["posicoes"] if int(p["id"]) != pid]


def limpar_simulacao_carteiras() -> None:
    """Zera posições A e B para nova simulação."""
    inicializar_estado_carteiras()
    for cid in CARTEIRAS_IDS:
        cart = st.session_state.carteiras[cid]
        cart["posicoes"] = []
        cart["next_id"] = 1
    for chave in list(st.session_state.keys()):
        if chave.startswith("cart_") and (
            chave.endswith("_novo_valor_pending")
            or chave.endswith("_novo_valor")
        ):
            del st.session_state[chave]
    st.session_state["_cart_reset_prazo"] = True


def _soma_valores_carteira(carteira_id: str) -> float:
    return sum(max(0.0, float(p.get("valor") or 0)) for p in _carteira_por_id(carteira_id)["posicoes"])


def linhas_resumo_de_consolidacao(
    cons: dict[str, Any],
    *,
    titulo_total: str,
    incluir_cdi: bool = False,
) -> list[dict[str, Any]]:
    """Linhas de resumo (total e opcionalmente CDI) para exibição."""
    linhas: list[dict[str, Any]] = []
    linhas.append(
        {
            "_linha": "total",
            "_pos_id": None,
            "Ativo": titulo_total,
            "Condição": str(cons.get("condicao_total", "—")),
            _COL_IR_EFETIVO: str(cons.get("ir_efetivo_total", "0%")),
            "% mês": f"{cons['taxa_carteira_mes'] * 100:.3f}",
            "% a.a.": f"{cons['taxa_carteira_liq_aa'] * 100:.2f}",
            "Montante": f"R$ {format_moeda_br(cons['montante_liquido'])}",
            "Recebido": f"R$ {format_moeda_br(cons['rendimento_liquido'])}",
        }
    )
    if incluir_cdi:
        linhas.append(
            {
                "_linha": "cdi",
                "_pos_id": None,
                "Ativo": "100% CDI (referência)",
                "Condição": f"CDI {cons['taxa_cdi_aa'] * 100:.2f}% a.a. · mercado BCB",
                _COL_IR_EFETIVO: "—",
                "% mês": f"{cons['taxa_cdi_mes'] * 100:.3f}",
                "% a.a.": f"{cons['taxa_cdi_aa'] * 100:.2f}",
                "Montante": f"R$ {format_moeda_br(cons['montante_cdi'])}",
                "Recebido": f"R$ {format_moeda_br(cons['rendimento_cdi'])}",
            }
        )
    return linhas


def montar_comparativo_duas_carteiras(
    cons_a: dict[str, Any],
    cons_b: dict[str, Any],
    *,
    nome_a: str,
    nome_b: str,
) -> dict[str, Any]:
    """Painel comparativo A vs B (métricas e linhas para tabela resumo)."""
    cap_a = cons_a["capital_alocado"]
    cap_b = cons_b["capital_alocado"]
    rec_a = cons_a["rendimento_liquido"]
    rec_b = cons_b["rendimento_liquido"]
    mont_a = cons_a["montante_liquido"]
    mont_b = cons_b["montante_liquido"]

    linhas_comp: list[dict[str, Any]] = []

    if cap_a > 0:
        linhas_comp.append(
            {
                "_linha": "total_a",
                "_pos_id": None,
                "Ativo": f"▸ {nome_a}",
                "Condição": str(cons_a.get("condicao_total", "—")),
                _COL_IR_EFETIVO: str(cons_a.get("ir_efetivo_total", "0%")),
                "% mês": f"{cons_a['taxa_carteira_mes'] * 100:.3f}",
                "% a.a.": f"{cons_a['taxa_carteira_liq_aa'] * 100:.2f}",
                "Montante": f"R$ {format_moeda_br(mont_a)}",
                "Recebido": f"R$ {format_moeda_br(rec_a)}",
            }
        )
    if cap_b > 0:
        linhas_comp.append(
            {
                "_linha": "total_b",
                "_pos_id": None,
                "Ativo": f"▸ {nome_b}",
                "Condição": str(cons_b.get("condicao_total", "—")),
                _COL_IR_EFETIVO: str(cons_b.get("ir_efetivo_total", "0%")),
                "% mês": f"{cons_b['taxa_carteira_mes'] * 100:.3f}",
                "% a.a.": f"{cons_b['taxa_carteira_liq_aa'] * 100:.2f}",
                "Montante": f"R$ {format_moeda_br(mont_b)}",
                "Recebido": f"R$ {format_moeda_br(rec_b)}",
            }
        )

    if cap_a > 0 and cap_b > 0:
        linhas_comp.append(
            {
                "_linha": "diff",
                "_pos_id": None,
                "Ativo": "▸ Diferença (B − A)",
                "Condição": "Recebido líquido e % do CDI",
                _COL_IR_EFETIVO: "—",
                "% mês": "—",
                "% a.a.": f"{(cons_b['taxa_carteira_liq_aa'] - cons_a['taxa_carteira_liq_aa']) * 100:+.2f}",
                "Montante": f"R$ {format_moeda_br(mont_b - mont_a)}",
                "Recebido": f"R$ {format_moeda_br(rec_b - rec_a)}",
            }
        )

    return {
        "linhas": linhas_comp,
        "rec_a": rec_a,
        "rec_b": rec_b,
        "diff_recebido": rec_b - rec_a,
        "pct_cdi_a": cons_a["pct_do_cdi"],
        "pct_cdi_b": cons_b["pct_do_cdi"],
        "cap_a": cap_a,
        "cap_b": cap_b,
    }


def estilizar_tabela_comparativo_carteiras(df: pd.DataFrame, tipos: list[str]) -> Any:
    cols_vis = list(df.columns)

    def _cor_linha(row: pd.Series) -> list[str]:
        tipo = tipos[row.name] if row.name < len(tipos) else ""
        if tipo == "cdi":
            return ["background-color: #eef4fc; color: #1a4b8c; font-weight: 600"] * len(cols_vis)
        if tipo == "total_a":
            return ["background-color: #f0f7f1; color: #1e5631; font-weight: 700"] * len(cols_vis)
        if tipo == "total_b":
            return ["background-color: #e8f0fa; color: #1a4b8c; font-weight: 700"] * len(cols_vis)
        if tipo == "diff":
            return ["background-color: #faf8f0; color: #5c4a12; font-weight: 600"] * len(cols_vis)
        return [""] * len(cols_vis)

    return df.style.apply(_cor_linha, axis=1)


_FRACS_CARTEIRA_UI = [0.12, 0.28, 0.07, 0.09, 0.09, 0.17, 0.14]
_FRACS_CARTEIRA_COM_EXCL = [0.045, *(_FRACS_CARTEIRA_UI)]

_COLS_COMPARATIVO_UI = ["Ativo", "Condição", "IR", "% mês", "% a.a.", "Montante", "Recebido"]
_COL_ALIGN_COMPARATIVO = ("left", "left", "center", "right", "right", "right", "right")


def exibir_tabela_comparativo_ativos(linhas: list[dict[str, str]]) -> None:
    """Tabela do comparativo: visual do st.dataframe, largura 100% (sem corte em zoom)."""
    from xml.sax.saxutils import escape

    colgroup = "".join(
        f'<col style="width:{w * 100:.1f}%;" />' for w in _FRACS_CARTEIRA_UI
    )
    head = "".join(
        f'<th class="hart-tbl-comp-th hart-tbl-comp-align-{a}">{escape(nome)}</th>'
        for nome, a in zip(_COLS_COMPARATIVO_UI, _COL_ALIGN_COMPARATIVO, strict=True)
    )
    body_rows: list[str] = []
    for i, linha in enumerate(linhas):
        zebra = " hart-tbl-comp-row-alt" if i % 2 else ""
        cells = "".join(
            f'<td class="hart-tbl-comp-td hart-tbl-comp-align-{a}">{escape(str(linha.get(col, "")))}</td>'
            for col, a in zip(_COLS_COMPARATIVO_UI, _COL_ALIGN_COMPARATIVO, strict=True)
        )
        body_rows.append(f'<tr class="hart-tbl-comp-row{zebra}">{cells}</tr>')
    tbody = "".join(body_rows)
    st.markdown(
        f'<div class="hart-tbl-comparativo-grid">'
        f'<table class="hart-tbl-comp-table"><colgroup>{colgroup}</colgroup>'
        f"<thead><tr>{head}</tr></thead><tbody>{tbody}</tbody></table></div>",
        unsafe_allow_html=True,
    )


def exibir_quadro_resultados_carteira(
    cons: dict[str, Any],
    *,
    carteira_id: str,
    prazo_meses: int,
    prazo_dias: int,
    mostrar_excluir: bool = True,
) -> None:
    """Quadro no estilo do comparativo, com botão excluir por ativo."""
    ativos = [ln for ln in cons["linhas"] if ln.get("_linha") == "ativo"]
    resumo = [ln for ln in cons["linhas"] if ln.get("_linha") != "ativo"]

    if mostrar_excluir:
        hc = st.columns(_FRACS_CARTEIRA_COM_EXCL, gap="small")
        for i, nome in enumerate(["", *_COLS_CARTEIRA_UI]):
            if nome:
                hc[i].markdown(f"**{_titulo_col_carteira(nome)}**", unsafe_allow_html=True)
            else:
                hc[i].markdown("")
    else:
        hc = st.columns(_FRACS_CARTEIRA_UI, gap="small")
        for i, nome in enumerate(_COLS_CARTEIRA_UI):
            hc[i].markdown(f"**{_titulo_col_carteira(nome)}**", unsafe_allow_html=True)

    for linha in ativos:
        pid = int(linha["_pos_id"])
        if mostrar_excluir:
            cc = st.columns(_FRACS_CARTEIRA_COM_EXCL, gap="small")
            with cc[0]:
                if st.button("✕", key=f"cart_{carteira_id}_excl_{pid}", help="Excluir ativo"):
                    _excluir_posicao_carteira(carteira_id, pid)
                    st.rerun()
            col_start = 1
        else:
            cc = st.columns(_FRACS_CARTEIRA_UI, gap="small")
            col_start = 0
        for j, col in enumerate(_COLS_CARTEIRA_UI, start=col_start):
            cc[j].markdown(str(linha.get(col, "")))
    if resumo:
        linhas_resumo: list[dict[str, str]] = []
        for ln in resumo:
            row = {c: str(ln.get(c, "—")) for c in _COLS_CARTEIRA_UI}
            linhas_resumo.append(row)
        df_res = pd.DataFrame(linhas_resumo)
        tipos = [ln.get("_linha", "") for ln in resumo]
        n = len(linhas_resumo)
        altura = min(200, max(90, 44 + n * 38))
        estilizar_fn = (
            estilizar_tabela_comparativo_carteiras
            if any(t in ("total_a", "total_b", "diff") for t in tipos)
            else estilizar_tabela_carteira_resumo
        )
        try:
            styled = estilizar_fn(df_res, tipos)
            st.dataframe(
                styled,
                use_container_width=True,
                hide_index=True,
                height=altura,
                column_config={
                    "Ativo": st.column_config.TextColumn("Ativo", width="medium"),
                    "Condição": st.column_config.TextColumn("Condição", width="large"),
                    _COL_IR_EFETIVO: st.column_config.TextColumn(
                        _LABEL_IR_EFETIVO, width="small"
                    ),
                    "% mês": st.column_config.TextColumn("% mês", width="small"),
                    "% a.a.": st.column_config.TextColumn("% a.a.", width="small"),
                    "Montante": st.column_config.TextColumn("Montante", width=None),
                    "Recebido": st.column_config.TextColumn("Recebido", width=None),
                },
            )
        except Exception:
            st.dataframe(
                df_res,
                use_container_width=True,
                hide_index=True,
                height=altura,
                column_config={
                    "Ativo": st.column_config.TextColumn("Ativo", width="medium"),
                    "Condição": st.column_config.TextColumn("Condição", width="large"),
                    _COL_IR_EFETIVO: st.column_config.TextColumn(
                        _LABEL_IR_EFETIVO, width="small"
                    ),
                    "% mês": st.column_config.TextColumn("% mês", width="small"),
                    "% a.a.": st.column_config.TextColumn("% a.a.", width="small"),
                    "Montante": st.column_config.TextColumn("Montante", width=None),
                    "Recebido": st.column_config.TextColumn("Recebido", width=None),
                },
            )


def render_formulario_carteira(carteira_id: str, titulo: str) -> None:
    """Formulário para incluir ativos em uma carteira (painel A ou B)."""
    cart = _carteira_por_id(carteira_id)
    for pos in cart["posicoes"]:
        _migrar_posicao_carteira(pos, 0.0)

    st.markdown(f"**{titulo}**")
    st.caption(f"Alocado: **R$ {format_moeda_br(_soma_valores_carteira(carteira_id))}**")

    _pend_key = f"cart_{carteira_id}_novo_valor_pending"
    _valor_key = f"cart_{carteira_id}_novo_valor"
    if _pend_key in st.session_state:
        st.session_state[_valor_key] = float(st.session_state.pop(_pend_key))
    elif _valor_key not in st.session_state:
        st.session_state[_valor_key] = 10_000.0

    rotulo_novo = st.selectbox(
        "Ativo",
        ROTULOS_CARTEIRA,
        key=f"cart_{carteira_id}_novo_rotulo",
    )
    eh_fundo_n = rotulo_novo == "Fundo DI"
    eh_pre_n = "pré" in rotulo_novo
    eh_poup_n = rotulo_novo == "Poupança"

    c_val, c_taxa = st.columns(2)
    with c_val:
        valor_novo = st.number_input(
            "Valor (R$)",
            min_value=0.0,
            max_value=1e12,
            step=1000.0,
            format="%.2f",
            key=_valor_key,
        )
    with c_taxa:
        if eh_poup_n:
            st.caption("Taxa automática")
            taxa_novo = 0.0
        else:
            taxa_novo = st.number_input(
                "Pré % a.a." if eh_pre_n else "% CDI",
                min_value=0.0,
                max_value=500.0,
                value=100.0 if not eh_pre_n else 12.0,
                step=0.05 if not eh_pre_n else 0.1,
                format="%.2f",
                key=f"cart_{carteira_id}_novo_taxa",
            )

    if eh_fundo_n:
        adm_novo = st.number_input(
            "Adm % a.a.",
            min_value=0.0,
            max_value=10.0,
            value=0.5,
            step=0.05,
            format="%.2f",
            key=f"cart_{carteira_id}_novo_adm",
        )
    else:
        adm_novo = 0.5

    if st.button("Incluir ativo", key=f"cart_{carteira_id}_incluir", use_container_width=True, type="primary"):
        if float(valor_novo) <= 0:
            st.warning("Informe um valor maior que zero.")
        else:
            nid = int(cart["next_id"])
            cart["next_id"] = nid + 1
            cart["posicoes"].append(
                {
                    "id": nid,
                    "rotulo": rotulo_novo,
                    "valor": float(valor_novo),
                    "taxa": float(taxa_novo),
                    "adm_fundo": float(adm_novo),
                }
            )
            st.session_state[_pend_key] = 0.0
            st.rerun()


_HELP_IR_PATRIMONIO = (
    "% IR no Patrimônio = IR pago ÷ (capital + rendimento bruto). "
    "Mostra o impacto real do imposto sobre o patrimônio bruto final da carteira, "
    "permitindo comparar carteiras com ativos isentos (LCI/LCA) e tributáveis."
)


def metric_carteira(
    col: Any,
    label: str,
    value: str,
    *,
    tema: str,
    help: str | None = None,
    delta: str | None = None,
    marker: str = "",
) -> None:
    """Métrica com marcador CSS (tema: a, b ou diff)."""
    extra = f" hart-metric-{marker}" if marker else ""
    col.markdown(
        f'<div class="hart-metric-marker hart-metric-{tema}{extra}" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    kwargs: dict[str, Any] = {}
    if help is not None:
        kwargs["help"] = help
    if delta is not None:
        kwargs["delta"] = delta
    col.metric(label, value, **kwargs)


def metricas_imposto_carteira(cons: dict[str, Any]) -> tuple[float, float]:
    """Retorna (IR pago, % IR no Patrimônio sobre patrimônio bruto final)."""
    ir_pago = float(cons.get("ir_total") or 0.0)
    patrimonio_bruto = float(cons.get("capital_alocado") or 0.0) + float(
        cons.get("rendimento_bruto") or 0.0
    )
    if patrimonio_bruto > 0:
        aliquota_efetiva = ir_pago / patrimonio_bruto * 100.0
    else:
        aliquota_efetiva = 0.0
    return ir_pago, aliquota_efetiva


def exibir_painel_resultados_carteiras(
    cons_a: dict[str, Any],
    cons_b: dict[str, Any],
    *,
    prazo_meses: int,
    prazo_dias: int,
    selic_meta_aa: float,
    cdi_aa: float,
) -> None:
    """Resultados: comparativo quando há duas carteiras; detalhe independente quando só uma."""
    tem_a = bool(cons_a["linhas"])
    tem_b = bool(cons_b["linhas"])
    nome_a = _NOMES_CARTEIRA["A"]
    nome_b = _NOMES_CARTEIRA["B"]

    if not tem_a and not tem_b:
        st.info("Inclua ao menos um ativo em **Carteira A** ou **Carteira B** acima.")
        return

    st.markdown("#### Resultados")

    if tem_a and tem_b:
        comp = montar_comparativo_duas_carteiras(
            cons_a, cons_b, nome_a=nome_a, nome_b=nome_b
        )
        ir_a, aliq_ef_a = metricas_imposto_carteira(cons_a)
        ir_b, aliq_ef_b = metricas_imposto_carteira(cons_b)
        _r1 = st.columns([1, 1, 1, 0.14, 1, 1, 1])
        metric_carteira(
            _r1[0],
            f"Recebido · {nome_a}",
            f"R$ {format_moeda_br(comp['rec_a'])}",
            tema="a",
            help="Lucro líquido total após IR.",
        )
        metric_carteira(
            _r1[1],
            f"IR pago · {nome_a}",
            f"R$ {format_moeda_br(ir_a)}",
            tema="a",
            help="Total de imposto retido na fonte (soma das posições).",
        )
        metric_carteira(
            _r1[2],
            f"% IR no Patrimônio · {nome_a}",
            f"{aliq_ef_a:.2f}%",
            tema="a",
            help=_HELP_IR_PATRIMONIO,
        )
        metric_carteira(
            _r1[4], f"Recebido · {nome_b}", f"R$ {format_moeda_br(comp['rec_b'])}", tema="b"
        )
        metric_carteira(_r1[5], f"IR pago · {nome_b}", f"R$ {format_moeda_br(ir_b)}", tema="b")
        metric_carteira(
            _r1[6],
            f"% IR no Patrimônio · {nome_b}",
            f"{aliq_ef_b:.2f}%",
            tema="b",
            help=_HELP_IR_PATRIMONIO,
        )
        _diff_rec = comp["diff_recebido"]
        _r2 = st.columns([1.05, 0.82, 0.82, 1.55, 1.05])
        metric_carteira(
            _r2[1],
            "Diferença recebido (B − A)",
            f"R$ {format_moeda_br(_diff_rec)}",
            tema="diff",
            delta=f"{_diff_rec:+.2f}" if abs(_diff_rec) > 0.01 else None,
        )
        metric_carteira(
            _r2[2],
            "Diferença IR pago (B − A)",
            f"R$ {format_moeda_br(ir_b - ir_a)}",
            tema="diff",
        )
        metric_carteira(
            _r2[3],
            _LABEL_CDI_LIQ_AB_METRIC,
            f"A {comp['pct_cdi_a']:.1f}% · B {comp['pct_cdi_b']:.1f}%",
            tema="diff",
            marker="cdi-ab",
            help=(
                f"{nome_a}: {_TXT_CDI_LIQ}: {comp['pct_cdi_a']:.1f}% · "
                f"{nome_b}: {_TXT_CDI_LIQ}: {comp['pct_cdi_b']:.1f}% — "
                "taxa líquida anual vs CDI BCB."
            ),
        )
        exibir_quadro_resultados_carteira(
            {"linhas": comp["linhas"]},
            carteira_id="cmp",
            prazo_meses=prazo_meses,
            prazo_dias=prazo_dias,
            mostrar_excluir=False,
        )
        st.caption(
            f"{prazo_meses} meses · {prazo_dias} dias (IR) · nominal líquido · "
            f"CDI {cons_a['taxa_cdi_aa'] * 100:.2f}% a.a. (BCB)"
        )
        st.markdown("##### Detalhe por carteira")
        _da, _db = st.columns(2)
        with _da:
            st.markdown(f"**{nome_a}**")
            exibir_quadro_resultados_carteira(
                cons_a,
                carteira_id="A",
                prazo_meses=prazo_meses,
                prazo_dias=prazo_dias,
                mostrar_excluir=True,
            )
        with _db:
            st.markdown(f"**{nome_b}**")
            exibir_quadro_resultados_carteira(
                cons_b,
                carteira_id="B",
                prazo_meses=prazo_meses,
                prazo_dias=prazo_dias,
                mostrar_excluir=True,
            )
        _exibir_download_pdf_carteira(
            prazo_meses=prazo_meses,
            prazo_dias=prazo_dias,
            nome_a=nome_a,
            nome_b=nome_b,
            cons_a=cons_a,
            cons_b=cons_b,
            comp=comp,
            comp_linhas=comp["linhas"],
            ir_a=ir_a,
            ir_b=ir_b,
            aliq_ef_a=aliq_ef_a,
            aliq_ef_b=aliq_ef_b,
            selic_meta_aa=selic_meta_aa,
            cdi_aa=cdi_aa,
        )
        return

    if tem_a:
        cons = cons_a
        nome = nome_a
        cid = "A"
    else:
        cons = cons_b
        nome = nome_b
        cid = "B"

    ir_pago, aliq_efetiva = metricas_imposto_carteira(cons)
    _tema_unica = "a" if cid == "A" else "b"
    _m1, _m2, _m3, _m4, _m5 = st.columns(5)
    metric_carteira(
        _m1, "Recebido", f"R$ {format_moeda_br(cons['rendimento_liquido'])}", tema=_tema_unica
    )
    metric_carteira(
        _m2, "Montante", f"R$ {format_moeda_br(cons['montante_liquido'])}", tema=_tema_unica
    )
    metric_carteira(_m3, "IR pago", f"R$ {format_moeda_br(ir_pago)}", tema=_tema_unica)
    metric_carteira(
        _m4, "% IR no Patrimônio", f"{aliq_efetiva:.2f}%", tema=_tema_unica, help=_HELP_IR_PATRIMONIO
    )
    metric_carteira(
        _m5,
        _TXT_CDI_LIQ,
        f"{cons['pct_do_cdi']:.1f}%",
        tema=_tema_unica,
        help=f"{_TXT_CDI_LIQ}: taxa líquida anual da carteira em relação ao CDI BCB.",
    )
    exibir_quadro_resultados_carteira(
        {
            "linhas": linhas_resumo_de_consolidacao(
                cons,
                titulo_total=f"▸ {nome}",
            )
        },
        carteira_id=f"{cid}_res",
        prazo_meses=prazo_meses,
        prazo_dias=prazo_dias,
        mostrar_excluir=False,
    )
    st.caption(
        f"{prazo_meses} meses · {prazo_dias} dias (IR) · nominal líquido · "
        f"CDI {cons['taxa_cdi_aa'] * 100:.2f}% a.a. (BCB)"
    )
    st.markdown("##### Detalhe por ativo")
    exibir_quadro_resultados_carteira(
        cons,
        carteira_id=cid,
        prazo_meses=prazo_meses,
        prazo_dias=prazo_dias,
        mostrar_excluir=True,
    )
    _exibir_download_pdf_carteira(
        prazo_meses=prazo_meses,
        prazo_dias=prazo_dias,
        nome_a=nome if cid == "A" else "",
        nome_b=nome if cid == "B" else "",
        cons_a=cons if cid == "A" else None,
        cons_b=cons if cid == "B" else None,
        comp=None,
        comp_linhas=None,
        ir_a=ir_pago if cid == "A" else 0.0,
        ir_b=ir_pago if cid == "B" else 0.0,
        aliq_ef_a=aliq_efetiva if cid == "A" else 0.0,
        aliq_ef_b=aliq_efetiva if cid == "B" else 0.0,
        selic_meta_aa=selic_meta_aa,
        cdi_aa=cdi_aa,
        titulo_resumo=nome,
    )
    if tem_a and not tem_b:
        st.caption("Carteira B está vazia — inclua ativos à direita para comparar.")
    elif tem_b and not tem_a:
        st.caption("Carteira A está vazia — inclua ativos à esquerda para comparar.")


def calcular_carteira_consolidada(
    posicoes: list[dict[str, Any]],
    *,
    prazo_meses: int,
    prazo_dias: int,
    cdi_aa: float,
    poupanca_taxa_mensal: float,
) -> dict[str, Any]:
    """Consolida posições: IR sobre lucro, totais por soma, taxa da carteira por composição inversa."""
    linhas_tabela: list[dict[str, str]] = []
    montante_liquido = 0.0
    capital_alocado = 0.0
    rend_bruto_total = 0.0
    rend_liquido_total = 0.0
    ir_total = 0.0
    poup_mes = float(poupanca_taxa_mensal)
    poup_aa_equiv = (1.0 + poup_mes) ** 12 - 1.0
    cdi_m = cdi_taxa_mensal(cdi_aa)
    n = max(0, int(prazo_meses))
    for pos in posicoes:
        capital_i = max(0.0, float(pos.get("valor") or 0.0))
        if capital_i <= 0:
            continue

        rotulo = str(pos["rotulo"])
        taxa_pos = float(pos.get("taxa") or 0.0)
        adm = float(pos.get("adm_fundo") or 0.5)
        info = posicao_para_info(
            rotulo,
            taxa_pos,
            cdi_aa=cdi_aa,
            poupanca_aa_equiv=(1.0 + poup_mes) ** 12 - 1.0,
            adm_fundo=adm,
        )
        pos.pop("aliquota_ir", None)

        i_mes = i_mes_de_info(
            info,
            cdi_aa_percent=cdi_aa,
            poupanca_taxa_mensal=poup_mes,
        )
        sim = simular_ativo_renda_fixa(
            capital_i,
            prazo_meses,
            i_mes=i_mes,
            isento=isento_de_info(info),
            prazo_dias=prazo_dias,
        )

        montante_liquido += sim["montante_liquido"]
        capital_alocado += capital_i
        rend_bruto_total += sim["lucro_bruto"]
        rend_liquido_total += sim["recebido"]
        ir_total += sim["imposto"]

        linhas_tabela.append(
            {
                "_linha": "ativo",
                "_pos_id": pos.get("id"),
                "Ativo": info["nome"],
                "Condição": condicao_posicao_carteira(
                    rotulo,
                    taxa_pos,
                    capital_i,
                    cdi_aa=cdi_aa,
                    adm_fundo=adm,
                    poupanca_aa_equiv=poup_aa_equiv,
                ),
                _COL_IR_EFETIVO: formatar_ir_efetivo(
                    sim["imposto"], sim["lucro_bruto"]
                ),
                "% mês": f"{sim['taxa_mensal_liq'] * 100:.3f}",
                "% a.a.": f"{sim['taxa_aa_liq'] * 100:.2f}",
                "Montante": f"R$ {format_moeda_br(sim['montante_liquido'])}",
                "Recebido": f"R$ {format_moeda_br(sim['recebido'])}",
            }
        )

    montante_cdi = capital_alocado * (1.0 + cdi_m) ** n
    rend_cdi = montante_cdi - capital_alocado
    taxa_cdi_aa = cdi_aa / 100.0

    if capital_alocado > 0 and n > 0:
        taxa_carteira_liq = (montante_liquido / capital_alocado) ** (12.0 / n) - 1.0
        taxa_carteira_mes = (montante_liquido / capital_alocado) ** (1.0 / n) - 1.0
    else:
        taxa_carteira_liq = 0.0
        taxa_carteira_mes = 0.0

    pct_do_cdi = (taxa_carteira_liq / taxa_cdi_aa * 100.0) if taxa_cdi_aa > 0 else 0.0
    carga_ir_pct = (ir_total / rend_bruto_total * 100.0) if rend_bruto_total > 0 else 0.0
    ir_efetivo_total = formatar_ir_efetivo(ir_total, rend_bruto_total)
    condicao_total = condicao_total_carteira(
        posicoes,
        capital_alocado=capital_alocado,
        rendimento_bruto=rend_bruto_total,
        prazo_meses=prazo_meses,
        cdi_aa=cdi_aa,
        poupanca_aa_equiv=poup_aa_equiv,
    )

    return {
        "linhas": linhas_tabela,
        "montante_liquido": montante_liquido,
        "rendimento_liquido": rend_liquido_total,
        "rendimento_bruto": rend_bruto_total,
        "ir_total": ir_total,
        "carga_tributaria_pct": carga_ir_pct,
        "condicao_total": condicao_total,
        "ir_efetivo_total": ir_efetivo_total,
        "pct_do_cdi": pct_do_cdi,
        "taxa_carteira_liq_aa": taxa_carteira_liq,
        "taxa_carteira_mes": taxa_carteira_mes,
        "taxa_cdi_aa": taxa_cdi_aa,
        "taxa_cdi_mes": cdi_m,
        "montante_cdi": montante_cdi,
        "rendimento_cdi": rend_cdi,
        "capital_alocado": capital_alocado,
    }


def estilizar_tabela_carteira_resumo(df: pd.DataFrame, tipos: list[str]) -> Any:
    """Destaque suave nas linhas Carteira (total) e 100% CDI."""
    cols_vis = list(df.columns)

    def _cor_linha(row: pd.Series) -> list[str]:
        tipo = tipos[row.name] if row.name < len(tipos) else ""
        if tipo == "cdi":
            return ["background-color: #eef4fc; color: #1a4b8c; font-weight: 600"] * len(cols_vis)
        if tipo == "total":
            return ["background-color: #f0f7f1; color: #1e5631; font-weight: 700"] * len(cols_vis)
        return [""] * len(cols_vis)

    return df.style.apply(_cor_linha, axis=1)


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


def _linhas_para_dataframe_carteira(linhas: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{c: str(ln.get(c, "")) for c in _COLS_CARTEIRA_UI} for ln in linhas]
    )


def _linhas_detalhe_carteira_pdf(
    cons: dict[str, Any],
    nome: str,
) -> list[dict[str, Any]]:
    """Ativos + linha de total para o PDF de detalhe."""
    return list(cons["linhas"]) + linhas_resumo_de_consolidacao(
        cons,
        titulo_total=f"Total — {nome}",
    )


def gerar_pdf_relatorio_carteiras(
    *,
    prazo_meses: int,
    prazo_dias: int,
    selic_meta_aa: float,
    cdi_aa: float,
    titulo_pdf: str,
    resumo_numeros_html: str,
    df_comparativo: pd.DataFrame | None,
    secoes_detalhe: list[dict[str, Any]],
) -> bytes:
    """PDF completo: visão geral, comparativo (se houver) e detalhe por carteira."""
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
        "tit_pdf",
        parent=styles["Normal"],
        fontName=font_b,
        fontSize=15,
        leading=19,
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    meta_style = ParagraphStyle(
        "meta_pdf",
        parent=styles["Normal"],
        fontName=font_n,
        fontSize=9,
        leading=13,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#444444"),
    )
    body_style = ParagraphStyle(
        "body_pdf",
        parent=styles["Normal"],
        fontName=font_n,
        fontSize=9,
        leading=13,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#333333"),
    )
    sec_style = ParagraphStyle(
        "sec_pdf",
        parent=styles["Normal"],
        fontName=font_b,
        fontSize=11,
        leading=14,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#1a2744"),
        spaceBefore=6,
        spaceAfter=4,
    )
    foot_style = ParagraphStyle(
        "foot_pdf",
        parent=styles["Normal"],
        fontName=font_n,
        fontSize=7,
        leading=10,
        textColor=colors.HexColor("#666666"),
    )
    th_style = ParagraphStyle(
        "th_pdf",
        parent=styles["Normal"],
        fontName=font_b,
        fontSize=8,
        leading=10,
        alignment=TA_CENTER,
    )
    td_left = ParagraphStyle(
        "tdl_pdf",
        parent=styles["Normal"],
        fontName=font_n,
        fontSize=7,
        leading=9,
        alignment=TA_LEFT,
    )
    td_right = ParagraphStyle(
        "tdr_pdf",
        parent=styles["Normal"],
        fontName=font_n,
        fontSize=7,
        leading=9,
        alignment=TA_CENTER,
    )

    def _p(txt: str, style: ParagraphStyle) -> Paragraph:
        return Paragraph(escape(str(txt)), style)

    def _p_cabecalho_col(txt: str) -> Paragraph:
        rot = _titulo_col_carteira_pdf(txt)
        if "<br" in rot:
            return Paragraph(rot, th_style)
        return Paragraph(escape(rot), th_style)

    def _adicionar_tabela(
        story: list[Any],
        df: pd.DataFrame,
        *,
        header_bg: str = "#e8e8e8",
        destaque_ultima: bool = False,
    ) -> None:
        if df.empty:
            return
        headers = list(df.columns)
        ncols = len(headers)
        fracs_7 = [0.10, 0.28, 0.08, 0.09, 0.09, 0.18, 0.18]
        col_widths = (
            [usable_w * f for f in fracs_7]
            if ncols == len(fracs_7)
            else [usable_w / ncols] * ncols
        )

        def _estilo_celula(nome_col: str) -> ParagraphStyle:
            if nome_col in ("Ativo", "Condição"):
                return td_left
            return td_right

        head_row = [_p_cabecalho_col(h) for h in headers]
        body_rows: list[list[Paragraph]] = []
        for row in df.values.tolist():
            cells = [_p(val, _estilo_celula(headers[j])) for j, val in enumerate(row)]
            body_rows.append(cells)
        tbl_data: list[list[Any]] = [head_row] + body_rows
        estilo_tbl = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg)),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        n_body = len(body_rows)
        if n_body > 1:
            estilo_tbl.append(
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -2 if destaque_ultima else -1),
                    [colors.white, colors.HexColor("#f8f8f8")],
                )
            )
        if destaque_ultima and n_body >= 1:
            estilo_tbl.append(
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef4fc"))
            )
            estilo_tbl.append(("FONTNAME", (0, -1), (-1, -1), font_b))
        t = Table(tbl_data, repeatRows=1, colWidths=col_widths)
        t.setStyle(TableStyle(estilo_tbl))
        story.append(t)

    buf = BytesIO()
    left_m = 1.8 * cm
    right_m = 1.8 * cm
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=left_m,
        rightMargin=right_m,
        topMargin=1.4 * cm,
        bottomMargin=1.4 * cm,
    )
    usable_w = A4[0] - left_m - right_m
    story: list[Any] = []

    story.append(Paragraph(escape(titulo_pdf), title_style))
    story.append(
        Paragraph(
            f"<b>Prazo da simulação:</b> {prazo_meses} meses ({prazo_dias} dias para IR)<br/>"
            f"<b>Mercado (BCB):</b> Selic meta {selic_meta_aa:.2f}% a.a. · CDI {cdi_aa:.2f}% a.a.",
            meta_style,
        )
    )
    story.append(Spacer(1, 0.35 * cm))
    story.append(
        Paragraph(
            "Este relatório mostra quanto cada carteira rendeu no período, "
            "já considerando imposto quando o investimento é tributável. "
            "Valores são <b>nominais líquidos</b> (após IR).",
            body_style,
        )
    )
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(resumo_numeros_html, body_style))

    if df_comparativo is not None and not df_comparativo.empty:
        story.append(Spacer(1, 0.45 * cm))
        story.append(Paragraph("1. Visão geral — comparativo", sec_style))
        story.append(
            Paragraph(
                "Resumo das duas carteiras lado a lado. A linha de diferença mostra o quanto "
                "a Carteira B ficou acima ou abaixo da Carteira A.",
                body_style,
            )
        )
        story.append(Spacer(1, 0.2 * cm))
        _adicionar_tabela(story, df_comparativo, header_bg="#dde4ef")

    for i, sec in enumerate(secoes_detalhe, start=2 if df_comparativo is not None else 1):
        linhas_sec = sec.get("linhas") or []
        if not linhas_sec:
            continue
        df_sec = _linhas_para_dataframe_carteira(linhas_sec)
        nome_sec = str(sec.get("nome", "Carteira"))
        story.append(Spacer(1, 0.45 * cm))
        if len(secoes_detalhe) > 1 or df_comparativo is not None:
            story.append(Paragraph(f"{i}. Detalhe — {escape(nome_sec)}", sec_style))
        else:
            story.append(Paragraph(f"{i}. Detalhe dos investimentos", sec_style))
        intro = str(
            sec.get(
                "intro",
                "Cada linha abaixo é um ativo que você incluiu, com valor aplicado e condição negociada.",
            )
        )
        story.append(Paragraph(intro, body_style))
        cap = sec.get("capital")
        if cap is not None and float(cap) > 0:
            story.append(
                Paragraph(
                    f"<b>Capital nesta carteira:</b> R$ {format_moeda_br(float(cap))}",
                    body_style,
                )
            )
        story.append(Spacer(1, 0.2 * cm))
        _adicionar_tabela(
            story,
            df_sec,
            header_bg=str(sec.get("cor_cabecalho", "#e8e8e8")),
            destaque_ultima=True,
        )

    story.append(Spacer(1, 0.5 * cm))
    story.append(
        Paragraph(
            "Fonte: séries SGS do Banco Central (API pública). Simulação educacional — "
            "não substitui assessoria profissional.",
            foot_style,
        )
    )

    doc.build(story)
    return buf.getvalue()


def _exibir_download_pdf_carteira(
    *,
    prazo_meses: int,
    prazo_dias: int,
    nome_a: str,
    nome_b: str,
    cons_a: dict[str, Any] | None,
    cons_b: dict[str, Any] | None,
    comp: dict[str, Any] | None,
    comp_linhas: list[dict[str, Any]] | None,
    ir_a: float,
    ir_b: float,
    aliq_ef_a: float,
    aliq_ef_b: float,
    selic_meta_aa: float,
    cdi_aa: float,
    titulo_resumo: str | None = None,
) -> None:
    """Gera PDF com comparativo, detalhe por carteira e botão de download."""
    secoes_detalhe: list[dict[str, Any]] = []
    df_comparativo: pd.DataFrame | None = None

    if comp is not None and comp_linhas and nome_a and nome_b:
        df_comparativo = _linhas_para_dataframe_carteira(comp_linhas)
        resumo = (
            f"<b>{nome_a} — {_TXT_CDI_LIQ}: {comp['pct_cdi_a']:.1f}%</b><br/>"
            f"Lucro líquido (Recebido): R$ {format_moeda_br(comp['rec_a'])}"
            f"<br/><br/>"
            f"<b>{nome_b} — {_TXT_CDI_LIQ}: {comp['pct_cdi_b']:.1f}%</b><br/>"
            f"Lucro líquido (Recebido): R$ {format_moeda_br(comp['rec_b'])}"
            f"<br/><br/>"
            f"<b>Diferença (B − A)</b><br/>"
            f"Recebido: R$ {format_moeda_br(comp['diff_recebido'])}"
        )
        titulo_pdf = "Relatório — Comparativo de carteiras"
        arquivo = "relatorio_comparativo_carteiras.pdf"
        if cons_a and cons_a.get("linhas"):
            secoes_detalhe.append(
                {
                    "nome": nome_a,
                    "linhas": _linhas_detalhe_carteira_pdf(cons_a, nome_a),
                    "cor_cabecalho": "#c8e6c9",
                    "capital": cons_a.get("capital_alocado"),
                    "intro": (
                        f"Investimentos incluídos na {nome_a}. "
                        "A última linha consolida o total da carteira."
                    ),
                }
            )
        if cons_b and cons_b.get("linhas"):
            secoes_detalhe.append(
                {
                    "nome": nome_b,
                    "linhas": _linhas_detalhe_carteira_pdf(cons_b, nome_b),
                    "cor_cabecalho": "#b8d4f0",
                    "capital": cons_b.get("capital_alocado"),
                    "intro": (
                        f"Investimentos incluídos na {nome_b}. "
                        "A última linha consolida o total da carteira."
                    ),
                }
            )
    else:
        cons = cons_a or cons_b
        nome = titulo_resumo or nome_a or nome_b or "Carteira"
        if cons is None or not cons.get("linhas"):
            return
        ir = ir_a or ir_b
        aliq = aliq_ef_a or aliq_ef_b
        resumo = (
            f"<b>{nome} — {_TXT_CDI_LIQ}: {cons['pct_do_cdi']:.1f}%</b><br/>"
            f"Lucro líquido (Recebido): R$ {format_moeda_br(cons['rendimento_liquido'])} · "
            f"Montante final: R$ {format_moeda_br(cons['montante_liquido'])}"
        )
        titulo_pdf = f"Relatório — {nome}"
        arquivo = f"relatorio_{nome.lower().replace(' ', '_')}.pdf"
        secoes_detalhe.append(
            {
                "nome": nome,
                "linhas": _linhas_detalhe_carteira_pdf(cons, nome),
                "cor_cabecalho": (
                    "#c8e6c9" if nome == _NOMES_CARTEIRA["A"] else "#b8d4f0"
                ),
                "capital": cons.get("capital_alocado"),
                "intro": (
                    "Lista de cada ativo com o valor que você aplicou. "
                    "A última linha mostra o total da carteira no período."
                ),
            }
        )

    if not secoes_detalhe and (df_comparativo is None or df_comparativo.empty):
        return

    try:
        pdf_bytes = gerar_pdf_relatorio_carteiras(
            prazo_meses=prazo_meses,
            prazo_dias=prazo_dias,
            selic_meta_aa=selic_meta_aa,
            cdi_aa=cdi_aa,
            titulo_pdf=titulo_pdf,
            resumo_numeros_html=resumo,
            df_comparativo=df_comparativo,
            secoes_detalhe=secoes_detalhe,
        )
        st.markdown("---")
        st.markdown('<motion class="hart-pdf-download-marker" />', unsafe_allow_html=True)
        st.download_button(
            label="Baixar PDF do relatório completo",
            data=pdf_bytes,
            file_name=arquivo,
            mime="application/pdf",
            help="PDF com comparativo e detalhe de cada carteira.",
            use_container_width=True,
            type="primary",
        )
    except Exception as e:
        st.error(f"Não foi possível gerar o PDF ({e}). Instale: pip install reportlab")


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
    .hart-modo-seletor {
        background: linear-gradient(135deg, #f4f7fc 0%, #eef2f9 100%);
        border: 1px solid #c5d0e6;
        border-radius: 12px;
        padding: 0.85rem 1rem 0.65rem 1rem;
        margin: 0.15rem 0 0.65rem 0;
        box-shadow: 0 1px 3px rgba(26, 75, 140, 0.06);
    }
    .hart-modo-seletor-titulo {
        font-size: 0.95rem;
        font-weight: 700;
        color: #1a2744;
        margin: 0 0 0.55rem 0;
        text-align: center;
        letter-spacing: 0.02em;
    }
    .hart-modo-card {
        background: #fff;
        border: 1px solid #d8e0ef;
        border-radius: 8px;
        padding: 0.55rem 0.65rem;
        min-height: 4.2rem;
        font-size: 0.78rem;
        line-height: 1.35;
        color: #334155;
    }
    .hart-modo-card strong {
        display: block;
        font-size: 0.86rem;
        color: #1a4b8c;
        margin-bottom: 0.2rem;
    }
    .hart-modo-card em {
        font-style: normal;
        font-size: 0.72rem;
        color: #64748b;
    }
    section.main [data-testid="stVerticalBlockBorderWrapper"]:has(.hart-modo-seletor) [data-testid="column"] button {
        min-height: 5.2rem !important;
        height: auto !important;
        white-space: pre-line !important;
        text-align: left !important;
        line-height: 1.38 !important;
        padding: 0.65rem 0.8rem !important;
        font-size: 0.78rem !important;
        font-weight: 500 !important;
        border-radius: 9px !important;
        box-shadow: none !important;
    }
    section.main [data-testid="stVerticalBlockBorderWrapper"]:has(.hart-modo-seletor) [data-testid="column"] button p {
        text-align: left !important;
        white-space: pre-line !important;
        line-height: 1.38 !important;
        font-size: 0.78rem !important;
    }
    section.main [data-testid="stVerticalBlockBorderWrapper"]:has(.hart-modo-seletor) [data-testid="column"] button[kind="primary"] {
        background: linear-gradient(135deg, #eef4fc 0%, #dce8f8 100%) !important;
        color: #0a2d5c !important;
        border: 2px solid #1a4b8c !important;
        box-shadow: 0 2px 10px rgba(26, 75, 140, 0.14) !important;
    }
    section.main [data-testid="stVerticalBlockBorderWrapper"]:has(.hart-modo-seletor) [data-testid="column"] button[kind="primary"] p {
        color: #0a2d5c !important;
        font-weight: 600 !important;
    }
    section.main [data-testid="stVerticalBlockBorderWrapper"]:has(.hart-modo-seletor) [data-testid="column"] button[kind="secondary"] {
        background: #fff !important;
        color: #334155 !important;
        border: 2px solid #d8e0ef !important;
    }
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
    /* Comparativo de ativos: coluna de resultados (~68%) encolhe no flex */
    section.main div[data-testid="stHorizontalBlock"]:has(.hart-col-resultados-comp) > div[data-testid="column"] {
        min-width: 0 !important;
    }
    /* Tabela comparativo — mesmo visual do st.dataframe (grid), largura fluida */
    .hart-tbl-comparativo-grid {
        width: 100%;
        max-width: 100%;
        margin: 0.15rem 0 0.45rem 0;
        border: 1px solid rgba(49, 51, 63, 0.12);
        border-radius: 0.5rem;
        overflow-x: auto;
        overflow-y: hidden;
        background: #fff;
        box-sizing: border-box;
    }
    .hart-tbl-comp-table {
        width: 100%;
        table-layout: fixed;
        border-collapse: collapse;
        font-size: 0.78rem;
        line-height: 1.2;
    }
    .hart-tbl-comp-table .hart-tbl-comp-th {
        background: #f0f2f6;
        color: rgba(49, 51, 63, 0.88);
        font-weight: 600;
        padding: 0.45rem 0.5rem;
        border-bottom: 1px solid rgba(49, 51, 63, 0.12);
        white-space: nowrap;
        vertical-align: middle;
    }
    .hart-tbl-comp-table .hart-tbl-comp-td {
        padding: 0.35rem 0.5rem;
        border-bottom: 1px solid rgba(49, 51, 63, 0.08);
        color: rgba(49, 51, 63, 0.92);
        vertical-align: middle;
        word-break: break-word;
        min-height: 22px;
    }
    .hart-tbl-comp-table .hart-tbl-comp-row-alt .hart-tbl-comp-td {
        background: #fafbfc;
    }
    .hart-tbl-comp-table tbody tr:last-child .hart-tbl-comp-td {
        border-bottom: none;
    }
    .hart-tbl-comp-align-left { text-align: left; }
    .hart-tbl-comp-align-center { text-align: center; }
    .hart-tbl-comp-align-right { text-align: right; }
    div[data-testid="stAppViewContainer"] .main {
        overflow-x: auto;
    }
    .hart-tbl-carteira-scroll {
        width: 100%;
        min-width: 0;
        overflow-x: auto;
        padding-bottom: 0.35rem;
    }
    .hart-tbl-carteira-scroll [data-testid="column"] {
        min-width: 0;
    }
    .hart-tbl-carteira-scroll [data-testid="column"] p,
    .hart-tbl-carteira-scroll [data-testid="column"] span {
        font-size: 0.76rem !important;
        line-height: 1.25 !important;
        word-break: break-word;
    }
    .hart-cart-cell-total {
        display: block;
        background: #d4edda;
        color: #155724;
        font-weight: 700;
        padding: 6px 4px;
        border-top: 2px solid #28a745;
        border-radius: 2px;
    }
    .hart-cart-cell-cdi {
        display: block;
        background: #b8d4f0;
        color: #0a2d5c;
        font-weight: 600;
        padding: 6px 4px;
        border-top: 2px solid #5b9bd5;
        border-radius: 2px;
    }
    .hart-metric-marker {
        display: none;
    }
    section.main [data-testid="column"]:has(.hart-metric-a) div[data-testid="stMetricLabel"],
    section.main [data-testid="column"]:has(.hart-metric-b) div[data-testid="stMetricLabel"],
    section.main [data-testid="column"]:has(.hart-metric-diff) div[data-testid="stMetricLabel"] {
        white-space: normal !important;
        line-height: 1.15 !important;
        overflow: visible !important;
    }
    section.main [data-testid="column"]:has(.hart-metric-a) {
        background: linear-gradient(135deg, #f0f7f1 0%, #e6f4ea 100%);
        border: 1px solid #b8dfc4;
        border-radius: 8px;
        padding: 0.2rem 0.4rem 0.3rem 0.4rem;
    }
    section.main [data-testid="column"]:has(.hart-metric-a) div[data-testid="stMetricLabel"] {
        color: #1e5631 !important;
        font-size: 0.62rem !important;
    }
    section.main [data-testid="column"]:has(.hart-metric-a) div[data-testid="stMetricValue"] {
        color: #155724 !important;
        font-size: 0.98rem !important;
        font-weight: 600 !important;
    }
    section.main [data-testid="column"]:has(.hart-metric-b) {
        background: linear-gradient(135deg, #e8f0fa 0%, #dce8f8 100%);
        border: 1px solid #b8cce8;
        border-radius: 8px;
        padding: 0.2rem 0.4rem 0.3rem 0.4rem;
    }
    section.main [data-testid="column"]:has(.hart-metric-b) div[data-testid="stMetricLabel"] {
        color: #1a4b8c !important;
        font-size: 0.62rem !important;
    }
    section.main [data-testid="column"]:has(.hart-metric-b) div[data-testid="stMetricValue"] {
        color: #0d3d7a !important;
        font-size: 0.98rem !important;
        font-weight: 600 !important;
    }
    section.main [data-testid="column"]:has(.hart-metric-diff) {
        background: linear-gradient(135deg, #faf8f0 0%, #f5efd8 100%);
        border: 1px solid #e0d4a8;
        border-radius: 8px;
        padding: 0.2rem 0.4rem 0.3rem 0.4rem;
    }
    section.main [data-testid="column"]:has(.hart-metric-diff) div[data-testid="stMetricLabel"] {
        color: #5c4a12 !important;
        font-size: 0.62rem !important;
    }
    section.main [data-testid="column"]:has(.hart-metric-cdi-ab) {
        min-width: 10.5rem;
        flex: 1.35 1 0 !important;
    }
    section.main [data-testid="column"]:has(.hart-metric-cdi-ab) div[data-testid="stMetricLabel"] {
        font-size: 0.58rem !important;
        line-height: 1.2 !important;
        white-space: pre-line !important;
    }
    section.main [data-testid="column"]:has(.hart-metric-diff) div[data-testid="stMetricValue"] {
        color: #4a3a0e !important;
        font-size: 0.98rem !important;
        font-weight: 600 !important;
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
    .hart-cart-linha {
        padding: 4px 0 6px 0;
        margin: 0 0 2px 0;
        border-bottom: 1px solid #e8e8ef;
    }
    .hart-cart-linha [data-testid="stNumberInput"] label p,
    .hart-cart-linha [data-testid="stSelectbox"] label p {
        font-size: 0.68rem !important;
        margin-bottom: 0 !important;
    }
    .hart-cart-linha [data-testid="stNumberInput"] input {
        max-width: 100% !important;
        min-height: 1.75rem !important;
        padding: 0.15rem 0.35rem !important;
        font-size: 0.78rem !important;
    }
    .hart-cart-linha [data-testid="stSelectbox"] > div > div {
        min-height: 1.75rem !important;
        font-size: 0.78rem !important;
    }
    .hart-cart-linha button {
        min-height: 1.75rem !important;
        padding: 0.1rem 0.35rem !important;
        font-size: 0.75rem !important;
    }
    .hart-topo-mercado-wrap {
        background: linear-gradient(135deg, #f8fafc 0%, #eef2f9 100%);
        border: 1px solid #d8e0ef;
        border-radius: 10px;
        padding: 0.45rem 0.75rem 0.55rem 0.75rem;
        margin: 0.1rem 0 0.55rem 0;
    }
    .hart-topo-mercado-wrap div[data-testid="stMetricValue"] {
        font-size: 1.22rem !important;
        font-weight: 600 !important;
    }
    .hart-topo-mercado-wrap div[data-testid="stMetricLabel"] {
        font-size: 0.72rem !important;
        text-transform: uppercase;
        letter-spacing: 0.03em;
    }
    .hart-painel-carteira {
        background: #fff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 0.65rem 0.75rem 0.75rem 0.75rem;
        min-height: 12rem;
    }
    section.main [data-testid="stVerticalBlock"]:has(.hart-pdf-download-marker)
        [data-testid="stDownloadButton"] button {
        background: linear-gradient(135deg, #1e5a9e 0%, #164a82 100%) !important;
        color: #ffffff !important;
        border: 1px solid #0f3d6e !important;
        font-weight: 600 !important;
        box-shadow: 0 2px 6px rgba(22, 74, 130, 0.28) !important;
    }
    section.main [data-testid="stVerticalBlock"]:has(.hart-pdf-download-marker)
        [data-testid="stDownloadButton"] button:hover {
        background: linear-gradient(135deg, #2569b0 0%, #1a5490 100%) !important;
        border-color: #0d3d7a !important;
    }
    section.main [data-testid="stVerticalBlock"]:has(.hart-pdf-download-marker)
        [data-testid="stDownloadButton"] button p {
        color: #ffffff !important;
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

def _migrar_posicao_carteira(pos: dict[str, Any], capital_ref: float) -> None:
    if "valor" not in pos and "alocacao_pct" in pos:
        pos["valor"] = max(0.0, capital_ref * float(pos.get("alocacao_pct") or 0) / 100.0)
    pos.pop("alocacao_pct", None)


_MODO_COMPARATIVO = "Comparativo de ativos"
_MODO_CARTEIRA = "Carteira"
if "modo_app" not in st.session_state:
    st.session_state.modo_app = _MODO_COMPARATIVO

with st.container(border=True):
    st.markdown(
        """
<div class="hart-modo-seletor">
  <p class="hart-modo-seletor-titulo">Escolha a modalidade de simulação</p>
</div>
""",
        unsafe_allow_html=True,
    )
    _lbl_comp = (
        "Comparativo de ativos\n"
        "Compare CDB, LCI/LCA, Poupança e Fundo DI com o mesmo capital e prazo — lado a lado."
    )
    _lbl_cart = (
        "Monte várias posições com valor individual por ativo e veja o consolidado da carteira."
    )
    _modo_c1, _modo_c2 = st.columns(2, gap="small")
    with _modo_c1:
        if st.button(
            _lbl_comp,
            key="hart_btn_modo_comparativo",
            use_container_width=True,
            type="primary" if st.session_state.modo_app == _MODO_COMPARATIVO else "secondary",
        ):
            if st.session_state.modo_app != _MODO_COMPARATIVO:
                st.session_state.modo_app = _MODO_COMPARATIVO
                st.rerun()
    with _modo_c2:
        if st.button(
            _lbl_cart,
            key="hart_btn_modo_carteira",
            use_container_width=True,
            type="primary" if st.session_state.modo_app == _MODO_CARTEIRA else "secondary",
        ):
            if st.session_state.modo_app != _MODO_CARTEIRA:
                st.session_state.modo_app = _MODO_CARTEIRA
                st.rerun()

modo_app = str(st.session_state.modo_app)
modo_carteira = modo_app == _MODO_CARTEIRA

st.divider()

if modo_carteira:
    inicializar_estado_carteiras()
    with st.container(border=True):
        _topo_esq, _topo_centro, _topo_dir = st.columns([0.15, 0.55, 0.30], gap="small")
        with _topo_centro:
            _tm1, _tm2 = st.columns(2)
            _tm1.metric("Selic meta", f"{selic_meta_aa:.2f}%", help="Série 432, % a.a. (BCB)")
            _tm2.metric("CDI (impl.)", f"{cdi_aa:.2f}%", help="A partir do CDI diário (série 12).")
        with _topo_dir:
            if st.session_state.pop("_cart_reset_prazo", False):
                st.session_state["cart_prazo_meses"] = 12
            _col_prazo, _col_limpar = st.columns([0.62, 0.38], gap="small")
            with _col_prazo:
                prazo_meses = int(
                    st.number_input(
                        "Prazo (meses)",
                        min_value=1,
                        max_value=600,
                        value=12,
                        step=1,
                        key="cart_prazo_meses",
                        help="Mesmo prazo para Carteira A e B.",
                    )
                )
            with _col_limpar:
                st.markdown("<div style='height:1.55rem'></div>", unsafe_allow_html=True)
                if st.button(
                    "Limpar",
                    key="cart_limpar",
                    use_container_width=True,
                    help="Remove todos os ativos das carteiras A e B e volta o prazo para 12 meses.",
                ):
                    limpar_simulacao_carteiras()
                    st.rerun()
    prazo_dias = int(prazo_meses * 30)

    _painel_a, _painel_b = st.columns(2, gap="medium")
    with _painel_a:
        with st.container(border=True):
            render_formulario_carteira("A", _NOMES_CARTEIRA["A"])
    with _painel_b:
        with st.container(border=True):
            render_formulario_carteira("B", _NOMES_CARTEIRA["B"])

    cons_a = calcular_carteira_consolidada(
        _carteira_por_id("A")["posicoes"],
        prazo_meses=prazo_meses,
        prazo_dias=prazo_dias,
        cdi_aa=cdi_aa,
        poupanca_taxa_mensal=tx_mes_poup,
    )
    cons_b = calcular_carteira_consolidada(
        _carteira_por_id("B")["posicoes"],
        prazo_meses=prazo_meses,
        prazo_dias=prazo_dias,
        cdi_aa=cdi_aa,
        poupanca_taxa_mensal=tx_mes_poup,
    )
    exibir_painel_resultados_carteiras(
        cons_a,
        cons_b,
        prazo_meses=prazo_meses,
        prazo_dias=prazo_dias,
        selic_meta_aa=selic_meta_aa,
        cdi_aa=cdi_aa,
    )

else:
    _frac_esq = 0.32
    col_param, col_result = st.columns([_frac_esq, 1.0 - _frac_esq], gap="small")

    with col_param:
        st.subheader("Parâmetros")
        valor_total = 0.0
        g1, g2 = st.columns(2)
        with g1:
            if "capital_br_pending" in st.session_state:
                st.session_state.capital_br_input = st.session_state.pop("capital_br_pending")
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
        with g2:
            prazo_meses = int(
                st.number_input("Prazo (meses)", min_value=1, max_value=600, value=12, step=1)
            )

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
        st.markdown(
            '<span class="hart-col-resultados-comp" aria-hidden="true"></span>',
            unsafe_allow_html=True,
        )
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

        prazo_dias = int(prazo_meses * 30)

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
                    cdi_aa_percent=cdi_aa,
                    poupanca_taxa_mensal=tx_mes_poup,
                    comparativo=True,
                )
            )
            if (
                negociacao_ativa
                and info["nome"] == ativo_negociado
                and "taxa_input" in info
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
                        cdi_aa_percent=cdi_aa,
                        poupanca_taxa_mensal=tx_mes_poup,
                        comparativo=True,
                    )
                )
                nego_linha_inserida = True

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
                        cdi_aa_percent=cdi_aa,
                        poupanca_taxa_mensal=tx_mes_poup,
                        comparativo=True,
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
            exibir_tabela_comparativo_ativos(resultados)
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
