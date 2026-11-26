import os
import json
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# --- CONFIGURAÇÃO ---
SPREADSHEET_ID = '1Gm8w8-hMVUp4zYV-W4fQ7J_T2alPf4zsR1DynwLRKNM'
ABA_DADOS = 'Dados'

def conectar_sheets():
    scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    sa_json_str = os.environ.get('GCP_SA_KEY')
    if sa_json_str:
        creds = Credentials.from_service_account_info(json.loads(sa_json_str), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file('credentials.json', scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(ABA_DADOS)
    return pd.DataFrame(sheet.get_all_records())

def parse_numero(val):
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace('R$', '').strip()
    if not s:
        return 0.0
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0

df = conectar_sheets()
df['VENDA'] = df['VENDA TOTAL'].apply(parse_numero)
df['CUSTO'] = df['CUSTO TOTAL'].apply(parse_numero)
df['QTD'] = pd.to_numeric(df['QTD'], errors='coerce').fillna(0).astype(int)

df['DT'] = pd.to_datetime(df['DATA'], format='%m/%d/%Y', errors='coerce')
mask_na = df['DT'].isna()
if mask_na.any():
    df.loc[mask_na, 'DT'] = pd.to_datetime(df.loc[mask_na, 'DATA'], dayfirst=True, errors='coerce')

df['ANO'] = df['DT'].dt.year.astype(str)
df['MES'] = df['DT'].dt.to_period('M').astype(str)

def agregar(dataframe, cols):
    g = dataframe.groupby(cols, as_index=False).agg(
        ped=('ID PEDIDO', 'count'),
        qtd=('QTD', 'sum'),
        rec=('VENDA', 'sum'),
        cst=('CUSTO', 'sum')
    )
    g['luc'] = g['rec'] - g['cst']
    g['mg'] = ((g['luc'] / g['rec']) * 100).fillna(0).round(2)
    g['rec'] = g['rec'].round(2)
    g['cst'] = g['cst'].round(2)
    g['luc'] = g['luc'].round(2)
    return g

rec_total = float(df['VENDA'].sum())
cst_total = float(df['CUSTO'].sum())
luc_total = rec_total - cst_total
mg_geral = round((luc_total / rec_total) * 100, 2) if rec_total > 0 else 0.0

# Série mensal com variação MoM pré-calculada (economiza raciocínio e tokens na IA)
por_mes = agregar(df, ['MES']).sort_values('MES').reset_index(drop=True)
por_mes['var_rec_pct'] = (por_mes['rec'].pct_change() * 100).round(2).fillna(0.0)
por_mes['var_mg_pp'] = por_mes['mg'].diff().round(2).fillna(0.0)

por_ano = agregar(df, ['ANO']).sort_values('ANO')
por_cat = agregar(df, ['CATEGORIA']).sort_values('rec', ascending=False)
por_prod = agregar(df, ['PRODUTO', 'CATEGORIA']).sort_values('rec', ascending=False).reset_index(drop=True)
por_prod['gap_mg_pp'] = (por_prod['mg'] - mg_geral).round(2)
por_reg = agregar(df, ['REGIÃO']).sort_values('rec', ascending=False)

# Evolução de clientes (últimos 3 meses vs 3 meses anteriores) pré-calculada
meses_ord = sorted(df['MES'].dropna().unique())
m_atual_3 = meses_ord[-3:]
m_ant_3 = meses_ord[-6:-3]

cli_geral = agregar(df, ['CLIENTE']).sort_values('rec', ascending=False)
cli_rec = df[df['MES'].isin(m_atual_3)].groupby('CLIENTE')['VENDA'].sum()
cli_ant = df[df['MES'].isin(m_ant_3)].groupby('CLIENTE')['VENDA'].sum()

cli_geral['rec_3m_atual'] = cli_geral['CLIENTE'].map(cli_rec).fillna(0).round(2)
cli_geral['rec_3m_ant'] = cli_geral['CLIENTE'].map(cli_ant).fillna(0).round(2)
cli_geral['delta_3m'] = (cli_geral['rec_3m_atual'] - cli_geral['rec_3m_ant']).round(2)

# Últimos 3 meses abertos por categoria e região
ultimos_3 = meses_ord[-3:]
cat_mes = agregar(df[df['MES'].isin(ultimos_3)], ['MES', 'CATEGORIA']).sort_values(['MES', 'rec'], ascending=[True, False])
reg_mes = agregar(df[df['MES'].isin(ultimos_3)], ['MES', 'REGIÃO']).sort_values(['MES', 'rec'], ascending=[True, False])

# Cards automáticos do topo
piores_prod = por_prod[por_prod['rec'] > (rec_total * 0.02)].sort_values('mg')
pior = piores_prod.iloc[0] if not piores_prod.empty else por_prod.sort_values('mg').iloc[0]
rank_pior = int(pior.name) + 1
lider_cat = por_cat.iloc[0]

def brl(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

insight_1 = (
    f"{pior['PRODUTO']} é o {rank_pior}º em receita ({brl(pior['rec'])}), "
    f"mas opera com margem de {pior['mg']:.1f}% ({pior['gap_mg_pp']:+.1f} p.p. vs. média de {mg_geral:.1f}%)."
)
insight_2 = (
    f"{lider_cat['CATEGORIA']} concentra {(lider_cat['rec']/rec_total)*100:.1f}% do faturamento "
    f"({brl(lider_cat['rec'])}) com margem média de {lider_cat['mg']:.1f}%."
)

saida = {
    "atualizado_em": pd.Timestamp.now(tz='America/Sao_Paulo').strftime('%d/%m/%Y %H:%M'),
    "cards": {"c1": insight_1, "c2": insight_2},
    "geral": {
        "ped": int(len(df)),
        "qtd": int(df['QTD'].sum()),
        "rec": round(rec_total, 2),
        "cst": round(cst_total, 2),
        "luc": round(luc_total, 2),
        "mg": mg_geral,
        "clientes": int(df['CLIENTE'].nunique()),
        "ultimo_mes": meses_ord[-1] if meses_ord else ""
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

print(f"dados_ia.json gerado com sucesso! ({len(df)} linhas | Receita: {brl(rec_total)} | Margem: {mg_geral}%)")
