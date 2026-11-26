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

def parse_margem(val):
    # Trata porcentagem vinda do Google Sheets (ex: 0.3234 ou "32,34%" ou 32.34)
    if isinstance(val, (int, float)):
        num = float(val)
        # Se o Sheets entregou em decimal (ex: 0.3234), converte para 32.34
        return num * 100.0 if -2.0 <= num <= 2.0 else num
    s = str(val).strip()
    tem_pct = '%' in s
    s = s.replace('%', '').strip()
    num = parse_numero(s)
    if not tem_pct and -2.0 <= num <= 2.0:
        return num * 100.0
    return num

df = conectar_sheets()
# Normaliza nomes das colunas para evitar erro por espaço ou maiúscula/minúscula
df.columns = [c.strip().upper() for c in df.columns]

df['VENDA'] = df['VENDA TOTAL'].apply(parse_numero)
df['MARGEM_LINHA'] = df['MARGEM'].apply(parse_margem)
# Calcula o lucro exato da linha a partir da venda e da margem da planilha
df['LUCRO'] = df['VENDA'] * (df['MARGEM_LINHA'] / 100.0)
df['CUSTO'] = df['VENDA'] - df['LUCRO']
df['QTD'] = pd.to_numeric(df['QTD'], errors='coerce').fillna(0).astype(int)

df['DT'] = pd.to_datetime(df['DATA'], format='%m/%d/%Y', errors='coerce')
mask_na = df['DT'].isna()
if mask_na.any():
    df.loc[mask_na, 'DT'] = pd.to_datetime(df.loc[mask_na, 'DATA'], dayfirst=True, errors='coerce')

df['ANO'] = df['DT'].dt.year.astype(str)
df['MES_ORD'] = df['DT'].dt.to_period('M').astype(str)

def agregar(dataframe, cols):
    g = dataframe.groupby(cols, as_index=False).agg(
        pedidos=('ID PEDIDO', 'count'),
        quantidade=('QTD', 'sum'),
        receita=('VENDA', 'sum'),
        custo=('CUSTO', 'sum'),
        lucro=('LUCRO', 'sum')
    )
    # Margem ponderada pela receita (bate 100% com o Looker Studio)
    g['margem_pct'] = ((g['lucro'] / g['receita']) * 100).fillna(0).round(2)
    g['receita'] = g['receita'].round(2)
    g['custo'] = g['custo'].round(2)
    g['lucro'] = g['lucro'].round(2)
    return g

rec_total = float(df['VENDA'].sum())
cst_total = float(df['CUSTO'].sum())
luc_total = float(df['LUCRO'].sum())
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

print(f"dados_ia.json atualizado! Receita: {brl(rec_total)} | Margem: {mg_geral}%")
