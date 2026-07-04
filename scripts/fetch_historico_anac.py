"""
fetch_historico_anac.py — Dados históricos ANAC/VRA + Supabase
Busca o arquivo VRA (Voo Regular Ativo) do portal de dados abertos da ANAC,
processa e insere na tabela historico_vra do Supabase.

Execução: mensal (1º dia de cada mês via GitHub Actions)

Variáveis de ambiente:
  SUPABASE_URL         → URL do projeto (GitHub Secret)
  SUPABASE_SERVICE_KEY → secret key / service_role key (GitHub Secret)
  AIRPORTS             → ICAOs para filtrar (GitHub Variable)
  ANO_MES              → Período a buscar no formato AAAA-MM
                         Padrão: mês anterior ao atual
"""

import csv
import io
import os
import sys
from datetime import datetime, timezone, timedelta

import requests
from supabase import create_client

# ── Credenciais ───────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("[ERRO CRÍTICO] SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórios.")
    sys.exit(1)

db = create_client(SUPABASE_URL, SUPABASE_KEY)
print(f"Supabase conectado: {SUPABASE_URL}")

# ── Configurações ─────────────────────────────────────────────────────────────

airports_env = os.environ.get("AIRPORTS", "SBCA")
AIRPORTS     = [a.strip().upper() for a in airports_env.split(",") if a.strip()]
LOTE         = 500

# Período: usa mês anterior por padrão (o VRA do mês atual fica disponível
# somente após o fechamento do mês)
BRT  = timezone(timedelta(hours=-3))
hoje = datetime.now(BRT)

if os.environ.get("ANO_MES"):
    ano_mes = os.environ["ANO_MES"].strip()  # ex: 2026-04
else:
    primeiro_do_mes = hoje.replace(day=1)
    mes_anterior    = primeiro_do_mes - timedelta(days=1)
    ano_mes         = mes_anterior.strftime("%Y-%m")

ano, mes = ano_mes.split("-")

print(f"Período histórico: {ano_mes}")
print(f"Aeroportos filtrados: {', '.join(AIRPORTS)}")

# ── URL do VRA ────────────────────────────────────────────────────────────────
# O portal da ANAC (servidor IIS) organiza os dados em pastas navegáveis.
# A estrutura pode variar: arquivo direto na pasta do ano, ou dentro de
# subpastas por mês (ex: "02 - fevereiro/"). Em vez de adivinhar o nome,
# o script navega recursivamente pela pasta do ano e localiza o .csv do mês.
_BASE = (
    "https://sistemas.anac.gov.br/dadosabertos/"
    "Voos e operações aéreas/Voo Regular Ativo (VRA)"
)

# User-Agent de navegador — o servidor da ANAC rejeita requests sem
# User-Agent (retorna 403).
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36"
}

PASTA_ANO = f"{_BASE}/{ano}/"

# Nome do mês por extenso (para casar subpastas tipo "02 - fevereiro")
MESES_PT = {
    "01": "janeiro", "02": "fevereiro", "03": "março", "04": "abril",
    "05": "maio", "06": "junho", "07": "julho", "08": "agosto",
    "09": "setembro", "10": "outubro", "11": "novembro", "12": "dezembro",
}

# Mapeamento de colunas do CSV do VRA
# Primeira entrada de cada lista = nome real no arquivo VRA/ANAC.
# As demais são alternativas de versões antigas ou outros portais.
COLS = {
    "empresa":      ["Sigla ICAO Empresa Aérea", "ICAO Empresa Aérea", "Icao",
                     "EMPRESA (SIGLA)", "Empresa (Sigla)", "sg_empresa_icao"],
    "voo":          ["Número Voo", "Numero Voo", "NÚMERO VOO", "nr_voo"],
    "origem":       ["ICAO Aeródromo Origem", "Aeródromo de Origem", "ORIGEM",
                     "Aeroporto Origem", "sg_icao_origem"],
    "destino":      ["ICAO Aeródromo Destino", "Aeródromo de Destino", "DESTINO",
                     "Aeroporto Destino", "sg_icao_destino"],
    "dt_ref":       ["Data Prevista", "DT_REFERENCIA", "Dt Referencia", "data_referencia"],
    "partida_prev": ["Partida Prevista", "PARTIDA PREVISTA", "dt_partida_prevista"],
    "partida_real": ["Partida Real",    "PARTIDA REAL",    "dt_partida_real"],
    "chegada_prev": ["Chegada Prevista","CHEGADA PREVISTA", "dt_chegada_prevista"],
    "chegada_real": ["Chegada Real",    "CHEGADA REAL",    "dt_chegada_real"],
    "situacao":     ["Situação Voo", "Situação", "SITUAÇÃO DE VOO", "Situacao Voo", "situacao"],
    "motivo":       ["Justificativa", "Código de Justificativa", "MOTIVO",
                     "Motivo Alteracao", "motivo_alteracao"],
}


def get_col(row: dict, key: str) -> str:
    """Tenta múltiplos nomes de coluna; fallback insensível a maiúsculas."""
    for nome in COLS.get(key, [key]):
        if nome in row and row[nome] is not None:
            return (row[nome] or "").strip()
    # Fallback case-insensitive — ignora chaves/valores None
    lower_map = {
        k.lower(): v
        for k, v in row.items()
        if isinstance(k, str)
    }
    for nome in COLS.get(key, [key]):
        val = lower_map.get(nome.lower())
        if val is not None:
            return (val or "").strip()
    return ""


def parse_dt_anac(dt_str: str) -> str | None:
    """Converte 'DD/MM/YYYY HH:MM' para ISO UTC."""
    if not dt_str or len(dt_str) < 16:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(dt_str.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def diff_minutos(partida_prev: str, partida_real: str) -> int | None:
    """Calcula atraso em minutos entre horário previsto e real."""
    try:
        fmt = "%d/%m/%Y %H:%M"
        dp  = datetime.strptime(partida_prev.strip(), fmt)
        dr  = datetime.strptime(partida_real.strip(), fmt)
        return int((dr - dp).total_seconds() / 60)
    except Exception:
        return None


# ── Busca o arquivo VRA ───────────────────────────────────────────────────────

def _ler_csv(conteudo: bytes) -> list[dict]:
    """
    Decodifica e faz o parse do CSV do VRA.

    Particularidades do arquivo VRA/ANAC:
      - A 1ª linha é um banner de metadados (ex: "Atualizado em: 2026-06-28"),
        NÃO o cabeçalho. O cabeçalho real vem na 2ª linha.
      - Separador ';'.
      - Codificação normalmente UTF-8 com BOM; latin-1 como fallback.
    """
    # Tenta decodificar priorizando UTF-8 com BOM (remove o ï»¿)
    texto = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            texto = conteudo.decode(enc)
            print(f"  Codificação usada: {enc}")
            break
        except Exception:
            continue
    if texto is None:
        texto = conteudo.decode("latin-1", errors="replace")

    linhas = texto.splitlines()
    if not linhas:
        print("  [ERRO] Arquivo vazio.")
        return []

    # Detecta a linha de cabeçalho: a que contém os nomes das colunas.
    # O banner de metadados não tem ';' repetido; o cabeçalho real tem
    # várias colunas separadas por ';'.
    idx_header = 0
    for i, ln in enumerate(linhas[:5]):
        if ln.count(";") >= 3:      # cabeçalho real tem muitos ';'
            idx_header = i
            break

    if idx_header > 0:
        print(f"  Ignorando {idx_header} linha(s) de metadados no topo "
              f"(ex: {linhas[0][:60]!r})")

    corpo = "\n".join(linhas[idx_header:])
    reader = csv.DictReader(io.StringIO(corpo), delimiter=";")
    registros = list(reader)

    print(f"  VRA carregado: {len(registros)} linhas de dados")
    if registros:
        cols = [c for c in registros[0].keys() if c]
        print(f"  Colunas detectadas: {cols}")
    return registros


def _listar_diretorio(url: str) -> tuple[list[str], list[str]]:
    """
    Lê uma pasta navegável do servidor IIS da ANAC e retorna
    (arquivos_csv, subpastas) como URLs absolutas.
    Funciona tanto com listagem estilo IIS quanto com HTML genérico.
    """
    import re
    from urllib.parse import urljoin, unquote

    if not url.endswith("/"):
        url += "/"
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=120)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"  [ERRO] Não foi possível listar {url}: {e}")
        return [], []

    # Captura todos os href da página
    hrefs = re.findall(r'href="([^"]+)"', html, flags=re.IGNORECASE)

    csvs, pastas = [], []
    for h in hrefs:
        if h.startswith("?") or h.startswith("#"):
            continue
        if "Parent Directory" in h or h in ("../", "/"):
            continue
        full = urljoin(url, h)
        # Ignora links que sobem na hierarquia
        if not full.startswith(_BASE):
            continue
        if full.lower().endswith(".csv"):
            csvs.append(full)
        elif full.endswith("/"):
            pastas.append(full)

    return sorted(set(csvs)), sorted(set(pastas))


def _procurar_csv_do_mes(url: str, profundidade: int = 0) -> str | None:
    """
    Navega recursivamente a partir da pasta do ano procurando um .csv
    que corresponda ao mês. Imprime a estrutura encontrada (diagnóstico).
    """
    indent = "  " * (profundidade + 1)
    print(f"{indent}Listando: {url}")
    csvs, pastas = _listar_diretorio(url)

    if csvs:
        print(f"{indent}.csv encontrados: {[c.split('/')[-1] for c in csvs][:12]}")
    if pastas:
        print(f"{indent}subpastas: {[p.rstrip('/').split('/')[-1] for p in pastas][:14]}")

    # Alvos de nome que indicam o mês certo
    mes_nome = MESES_PT.get(mes, "")
    mes_sz = str(int(mes))  # mês sem zero à esquerda (ex: "4" para abril)
    alvos = [f"{ano}_{mes}", f"{ano}{mes}", f"{ano}-{mes}",
             f"{mes}_{ano}", f"{mes}-{ano}",
             f"{ano}_{mes_sz}", f"{ano}{mes_sz}"]  # padrão VRA_20264.csv

    # 1) Algum .csv nesta pasta bate com o mês?
    for c in csvs:
        base = c.split("/")[-1].lower()
        if any(a in base for a in alvos):
            print(f"{indent}→ Arquivo do mês localizado: {c.split('/')[-1]}")
            return c

    # 2) Se só há um csv e a pasta já é a do mês, aceita
    if len(csvs) == 1 and profundidade > 0:
        print(f"{indent}→ Único .csv na pasta do mês: {csvs[0].split('/')[-1]}")
        return csvs[0]

    # 3) Desce em subpastas que correspondam ao mês (ex: "02 - fevereiro")
    if profundidade < 2:
        for p in pastas:
            nome_pasta = p.rstrip("/").split("/")[-1].lower()
            casa_mes = (
                nome_pasta.startswith(mes) or
                (mes_nome and mes_nome in nome_pasta) or
                any(a in nome_pasta for a in alvos)
            )
            if casa_mes:
                achado = _procurar_csv_do_mes(p, profundidade + 1)
                if achado:
                    return achado

    return None


def baixar_vra() -> list[dict]:
    print(f"\n[BUSCA] Procurando VRA de {ano}-{mes} a partir da pasta do ano.")
    url_csv = _procurar_csv_do_mes(PASTA_ANO, profundidade=0)

    if not url_csv:
        print(f"\n[ERRO] Nenhum .csv correspondente a {ano}-{mes} foi localizado "
              f"na estrutura de pastas do portal ANAC.")
        return []

    print(f"\nGET {url_csv}")
    try:
        r = requests.get(url_csv, headers=HTTP_HEADERS, timeout=180)
        r.raise_for_status()
        return _ler_csv(r.content)
    except Exception as e:
        print(f"  [ERRO] Falha ao baixar o arquivo: {e}")
        return []


# ── Processa e filtra registros ───────────────────────────────────────────────

def processar_vra(linhas: list[dict]) -> list[dict]:
    resultado = []
    for row in linhas:
        origem  = get_col(row, "origem").upper()
        destino = get_col(row, "destino").upper()
        if origem not in AIRPORTS and destino not in AIRPORTS:
            continue

        empresa       = get_col(row, "empresa")
        nr_voo        = get_col(row, "voo")
        dt_ref_str    = get_col(row, "dt_ref")
        partida_prev  = get_col(row, "partida_prev")
        partida_real  = get_col(row, "partida_real")
        chegada_prev  = get_col(row, "chegada_prev")
        chegada_real  = get_col(row, "chegada_real")
        situacao      = get_col(row, "situacao")
        motivo        = get_col(row, "motivo")

        # Data de referência
        dt_ref = None
        if dt_ref_str:
            try:
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        dt_ref = datetime.strptime(dt_ref_str.strip(), fmt).date().isoformat()
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        resultado.append({
            "ano_mes":          ano_mes,
            "icao_empresa":     empresa or None,
            "nr_voo":           nr_voo or None,
            "icao_origem":      origem or None,
            "icao_destino":     destino or None,
            "dt_referencia":    dt_ref,
            "partida_real":     parse_dt_anac(partida_real),
            "chegada_real":     parse_dt_anac(chegada_real),
            "atraso_partida":   diff_minutos(partida_prev, partida_real),
            "atraso_chegada":   diff_minutos(chegada_prev, chegada_real),
            "situacao":         situacao.lower() if situacao else None,
            "motivo_alteracao": motivo or None,
        })

    # Deduplica por chave única antes do upsert para evitar:
    # "ON CONFLICT DO UPDATE command cannot affect row a second time"
    # (ocorre quando o CSV contém a mesma linha mais de uma vez)
    _seen: set = set()
    _deduped: list = []
    for _r in resultado:
        _key = (
            _r["ano_mes"], _r["icao_empresa"], _r["nr_voo"],
            _r["icao_origem"], _r["icao_destino"], _r["dt_referencia"],
        )
        if _key not in _seen:
            _seen.add(_key)
            _deduped.append(_r)
    if len(_deduped) < len(resultado):
        print(f"  {len(resultado) - len(_deduped)} duplicata(s) removida(s) do lote.")
    print(f"  Registros filtrados para os aeroportos configurados: {len(_deduped)}")
    return _deduped


# ── Inserção no Supabase ──────────────────────────────────────────────────────

linhas_vra  = baixar_vra()
if not linhas_vra:
    print("\n[ERRO] VRA não disponível para o período em nenhum dos padrões testados.")
    print("       Verifique se o arquivo existe no portal da ANAC para o mês solicitado.")
    # exit(1) faz o workflow FALHAR (ícone vermelho) em vez de passar
    # silenciosamente verde sem importar nada.
    sys.exit(1)

registros   = processar_vra(linhas_vra)

if not registros:
    print("\n[ERRO] Nenhum registro após filtrar pelos aeroportos configurados.")
    print(f"       Aeroportos: {', '.join(AIRPORTS)}")
    print("       O arquivo VRA foi baixado mas nenhum voo bateu com os ICAOs.")
    sys.exit(1)
processados = 0
erros       = 0

for i in range(0, len(registros), LOTE):
    lote     = registros[i:i + LOTE]
    num_lote = i // LOTE + 1
    try:
        db.table("historico_vra").upsert(
            lote,
            on_conflict="ano_mes,icao_empresa,nr_voo,icao_origem,icao_destino,dt_referencia",
        ).execute()
        processados += len(lote)
        print(f"  Lote {num_lote}: {len(lote)} registros enviados/processados")
    except Exception as e:
        erros += 1
        print(f"  [ERRO] Lote {num_lote}: {e}")

print(f"\nConcluído — {processados} registros históricos enviados/processados.")
if erros > 0:
    print(f"[ATENÇÃO] {erros} lote(s) com erro.")
    sys.exit(1)
