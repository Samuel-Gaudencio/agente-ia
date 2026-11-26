import os
import json
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# --- CONFIGURAÇÃO ---
SPREADSHEET_ID = '1Gm8w8-hMVUp4zYV-W4fQ7J_T2alPf4zsR1DynwLRKNM'
ABA_DADOS = 'Dados'

MESES_PT = {
    1: 'janeiro', 2: 'fevereiro', 3: 'março', 4: 'abril',
    5: 'maio', 6: 'junho', 7: 'julho', 8: 'agosto',
    9: 'setembro', 10: 'outubro', 11: 'novembro', 12: 'dezembro'
}

def mes_extenso(periodo_str):
    ano, mes = periodo_str.split('-')
    return f"{MESES_PT[int(mes)]}/{ano}"

def conectar_sheets():
    scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    sa_json_str = os.environ.get('GCP_SA_KEY')
    if sa_json_str:
        creds = Credentials.from_service_account_info(json.loads(sa_json_str), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file('credentials.json', scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(ABA_DADOS)
    # UNFORMATTED_VALUE traz os floats exatos do banco da planilha (ex: 1031.8, 423.04)
    return pd.DataFrame(sheet.get_all_records(value_render_option='UNFORMATTED_VALUE'))

df = conectar_sheets()
df.columns = [c.strip().upper() for c in df.columns]

df['VENDA'] = pd.to_numeric(df['VENDA TOTAL'], errors='coerce').fillna(0.0)
df['CUSTO'] = pd.to_numeric(df['CUSTO TOTAL'], errors='coerce').fillna(0.0)
df['QTD'] = pd.to_numeric(df['QTD'], errors='coerce').fillna(0).astype(int)

# --- AUDITORIA EXATA DA COLUNA DATA E PEDIDOS ---
def converter_data_robusta(val):
    if val is None or str(val).strip() == '':
        return pd.NaT
    # 1. Número serial do Google Sheets (ex: 45658)
    if isinstance(val, (int, float)):
        return pd.to_datetime(val, unit='D', origin='1899-12-30')
    s = str(val).strip()
    # 2. Tenta formato americano (M/D/YYYY) que apareceu no seu print
    dt = pd.to_datetime(s, format='%m/%d/%Y', errors='coerce')
    if pd.notna(dt):
        return dt
    # 3. Fallback para formato brasileiro (D/M/YYYY) ou ISO (YYYY-MM-DD)
    return pd.to_datetime(s, dayfirst=True, errors='coerce')

df['DT'] = df['DATA'].apply(converter_data_robusta)

print(f"Total de linhas lidas: {len(df)}")
print(f"Datas inválidas/vazias: {df['DT'].isna().sum()}")
print(f"ID PEDIDO únicos no total: {df['ID PEDIDO'].nunique()}")

df = df[df['DT'].notna()].copy()
df['ANO'] = df['DT'].dt.year.astype(str)

print("\n--- COMPARATIVO POR ANO (PYTHON vs LOOKER) ---")
resumo_ano = df.groupby('ANO').agg(
    linhas=('ID PEDIDO', 'count'),
    pedidos_unicos=('ID PEDIDO', 'nunique'),
    venda_total=('VENDA', 'sum'),
    custo_total=('CUSTO', 'sum')
)
resumo_ano['margem_pct'] = ((resumo_ano['venda_total'] - resumo_ano['custo_total']) / resumo_ano['venda_total']) * 100
print(resumo_ano.to_string())

# No UNFORMATTED_VALUE, datas do Google Sheets vêm como serial numérico (dias desde 30/12/1899) ou string
def parse_data_sheets(val):
    if isinstance(val, (int, float)):
        return pd.to_datetime(val, unit='D', origin='1899-12-30')
    dt = pd.to_datetime(str(val), dayfirst=True, errors='coerce')
    if pd.isna(dt):
        dt = pd.to_datetime(str(val), format='%m/%d/%Y', errors='coerce')
    return dt

df['DT'] = df['DATA'].apply(parse_data_sheets)
df['ANO'] = df['DT'].dt.year.astype(str)
df['MES_ORD'] = df['DT'].dt.to_period('M').astype(str)

def agregar(dataframe, cols):
    g = dataframe.groupby(cols, as_index=False).agg(
        pedidos=('ID PEDIDO', 'count'),
        quantidade=('QTD', 'sum'),
        receita=('VENDA', 'sum'),
        custo=('CUSTO', 'sum')
    )
    g['lucro'] = g['receita'] - g['custo']
    # Fórmula idêntica ao Looker Studio: (SUM(VENDA TOTAL) - SUM(CUSTO TOTAL)) / SUM(VENDA TOTAL)
    g['margem_pct'] = ((g['lucro'] / g['receita']) * 100).fillna(0).round(2)
    g['receita'] = g['receita'].round(2)
    g['custo'] = g['custo'].round(2)
    g['lucro'] = g['lucro'].round(2)
    return g

rec_total = float(df['VENDA'].sum())
cst_total = float(df['CUSTO'].sum())
luc_total = rec_total - cst_total
mg_geral = round((luc_total / rec_total) * 100, 2) if rec_total > 0 else 0.0

por_mes = agregar(df, ['MES_ORD']).sort_values('MES_ORD').reset_index(drop=True)
por_mes['variacao_receita_mes_anterior_pct'] = (por_mes['receita'].pct_change() * 100).round(2).fillna(0.0)
por_mes['variacao_margem_mes_anterior_pp'] = por_mes['margem_pct'].diff().round(2).fillna(0.0)
por_mes['periodo'] = por_mes['MES_ORD'].apply(mes_extenso)
por_mes = por_mes.drop(columns=['MES_ORD'])

por_ano = agregar(df, ['ANO']).rename(columns={'ANO': 'ano'}).sort_values('ano')
por_cat = agregar(df, ['CATEGORIA']).rename(columns={'CATEGORIA': 'categoria'}).sort_values('receita', ascending=False)
por_prod = agregar(df, ['PRODUTO', 'CATEGORIA']).rename(columns={'PRODUTO': 'produto', 'CATEGORIA': 'categoria'}).sort_values('receita', ascending=False).reset_index(drop=True)
por_prod['diferenca_margem_media_pp'] = (por_prod['margem_pct'] - mg_geral).round(2)
por_reg = agregar(df, ['REGIÃO']).rename(columns={'REGIÃO': 'estado'}).sort_values('receita', ascending=False)

meses_ord = sorted(df['MES_ORD'].dropna().unique())
m_atual_3 = meses_ord[-3:]
m_ant_3 = meses_ord[-6:-3]

cli_geral = agregar(df, ['CLIENTE']).rename(columns={'CLIENTE': 'cliente'}).sort_values('receita', ascending=False)
cli_rec = df[df['MES_ORD'].isin(m_atual_3)].groupby('CLIENTE')['VENDA'].sum()
cli_ant = df[df['MES_ORD'].isin(m_ant_3)].groupby('CLIENTE')['VENDA'].sum()

cli_geral['receita_ultimos_3_meses'] = cli_geral['cliente'].map(cli_rec).fillna(0).round(2)
cli_geral['receita_3_meses_anteriores'] = cli_geral['cliente'].map(cli_ant).fillna(0).round(2)
cli_geral['crescimento_ultimos_3_meses'] = (cli_geral['receita_ultimos_3_meses'] - cli_geral['receita_3_meses_anteriores']).round(2)

ultimos_3 = meses_ord[-3:]
cat_mes = agregar(df[df['MES_ORD'].isin(ultimos_3)], ['MES_ORD', 'CATEGORIA']).sort_values(['MES_ORD', 'receita'], ascending=[True, False])
cat_mes['periodo'] = cat_mes['MES_ORD'].apply(mes_extenso)
cat_mes = cat_mes.rename(columns={'CATEGORIA': 'categoria'}).drop(columns=['MES_ORD'])

reg_mes = agregar(df[df['MES_ORD'].isin(ultimos_3)], ['MES_ORD', 'REGIÃO']).sort_values(['MES_ORD', 'receita'], ascending=[True, False])
reg_mes['periodo'] = reg_mes['MES_ORD'].apply(mes_extenso)
reg_mes = reg_mes.rename(columns={'REGIÃO': 'estado'}).drop(columns=['MES_ORD'])

piores_prod = por_prod[por_prod['receita'] > (rec_total * 0.02)].sort_values('margem_pct')
pior = piores_prod.iloc[0] if not piores_prod.empty else por_prod.sort_values('margem_pct').iloc[0]
rank_pior = int(pior.name) + 1
lider_cat = por_cat.iloc[0]

def brl(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

insight_1 = (
    f"{pior['produto']} é o {rank_pior}º em receita ({brl(pior['receita'])}), "
    f"mas opera com margem de {pior['margem_pct']:.1f}% ({pior['diferenca_margem_media_pp']:+.1f} p.p. vs. média de {mg_geral:.1f}%)."
)
insight_2 = (
    f"{lider_cat['categoria']} concentra {(lider_cat['receita']/rec_total)*100:.1f}% do faturamento "
    f"({brl(lider_cat['receita'])}) com margem média de {lider_cat['margem_pct']:.1f}%."
)

ultimo_mes_ext = mes_extenso(meses_ord[-1]) if meses_ord else ""

saida = {
    "atualizado_em": pd.Timestamp.now(tz='America/Sao_Paulo').strftime('%d/%m/%Y %H:%M'),
    "cards": {"c1": insight_1, "c2": insight_2},
    "geral": {
        "total_pedidos": int(len(df)),
        "quantidade_itens": int(df['QTD'].sum()),
        "receita_total": round(rec_total, 2),
        "custo_total": round(cst_total, 2),
        "lucro_total": round(luc_total, 2),
        "margem_media_geral_pct": mg_geral,
        "clientes_unicos": int(df['CLIENTE'].nunique()),
        "ultimo_mes_disponivel": ultimo_mes_ext
    },
    "ano": por_ano.to_dict(orient='records'),
    "mes": por_mes.to_dict(orient='records'),
    "cat": por_cat.to_dict(orient='records'),
    "prod": por_prod.to_dict(orient='records'),
    "reg": por_reg.to_dict(orient='records'),
    "cat_3m": cat_mes.to_dict(orient='records'),
    "reg_3m": reg_mes.to_dict(orient='records'),
    "cli": cli_geral.to_dict(orient='records')
}

with open('dados_ia.json', 'w', encoding='utf-8') as f:
    json.dump(saida, f, ensure_ascii=False, separators=(',', ':'))

print(f"OK! Receita: {brl(rec_total)} | Custo: {brl(cst_total)} | Margem: {mg_geral}%")
