#!/usr/bin/env python3
"""Baixa a legislação federal brasileira e a converte em JSON + Markdown.

Fontes (oficiais, CC-BY 4.0):
  - listagem e URN: API de Dados Abertos do Senado (legis.senado.leg.br/dadosabertos)
  - metadados e texto: normas.leg.br (portal do Congresso Nacional)

Uso:  python leis.py --tipos LEI,LCP --ano-ini 2020 --ano-fim 2020
Rodar de novo o mesmo comando retoma de onde parou (estado em <out>/state.sqlite).
"""
import argparse
import datetime
import html.parser
import http.client
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SENADO = "https://legis.senado.leg.br/dadosabertos/legislacao"
NORMAS = "https://normas.leg.br/api/public"
UA = "scrapping-leis-brasil/0.1 (+https://github.com/leoccker/scrapping_leis_brasil)"

# ---------------------------------------------------------------- HTTP

_lock = threading.Lock()
_next_t = 0.0
RPS = 4.0


class NotFound(Exception):
    pass


def _throttle():
    """Limite global de requisições por segundo, entre todas as threads."""
    global _next_t
    with _lock:
        now = time.monotonic()
        wait = _next_t - now
        _next_t = max(now, _next_t) + 1 / RPS
    if wait > 0:
        time.sleep(wait)


def get(url, accept="application/json", tries=6):
    for n in range(tries):
        _throttle()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url) from None
            if (e.code != 429 and e.code < 500) or n == tries - 1:
                raise
            retry_after = e.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else 2 ** n
        except (urllib.error.URLError, OSError, http.client.HTTPException):  # timeout, conexão caída, resposta truncada
            if n == tries - 1:
                raise
            delay = 2 ** n
        time.sleep(min(60, delay) + random.random())


def get_json(url):
    body = get(url)
    return json.loads(body) if body.strip() else None


# ---------------------------------------------------------------- parser

class _Paragrafos(html.parser.HTMLParser):
    """Extrai o texto de cada <p>/<h*>/<td> do HTML; marca o que está tachado (revogado)."""
    BLOCOS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "div", "br"}
    MUDOS = {"style", "script", "head", "title"}
    TACHADO = {"s", "strike", "del"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.buf, self.riscados = [], [], 0
        self.mudo, self.abertos = 0, []  # abertos: pilha (tag, é_tachado)

    def _fecha(self):
        texto = re.sub(r"\s+", " ", "".join(self.buf).replace("\xa0", " ")).strip()
        if texto:  # só conta como revogado se o parágrafo INTEIRO estiver tachado
            self.out.append((texto, self.riscados >= sum(len(b.strip()) for b in self.buf)))
        self.buf, self.riscados = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.MUDOS:
            self.mudo += 1
        if tag in self.BLOCOS:
            self._fecha()
        if tag not in ("br", "img", "meta", "link", "hr", "input"):
            estilo = dict(attrs).get("style") or ""
            self.abertos.append((tag, tag in self.TACHADO or "line-through" in estilo))

    def handle_endtag(self, tag):
        if tag in self.MUDOS:
            self.mudo = max(0, self.mudo - 1)
        if tag in self.BLOCOS:
            self._fecha()
        for i in range(len(self.abertos) - 1, -1, -1):
            if self.abertos[i][0] == tag:
                del self.abertos[i:]
                break

    def handle_data(self, data):
        if self.mudo:
            return
        self.buf.append(data)
        if any(t for _, t in self.abertos):
            self.riscados += len(data.strip())

    def close(self):
        super().close()
        self._fecha()


def extrair_paragrafos(html_text):
    p = _Paragrafos()
    p.feed(html_text.lstrip("\ufeff"))
    p.close()
    return p.out


SUF = r"(?:-([A-Z]{1,2}))?"  # sufixo de dispositivo acrescido: Art. 1º-A
RE_ART = re.compile(r"^Art(?:igo)?\.?\s*(\d+(?:\.\d{3})*)\s*[º°o]?\s*" + SUF + r"(?=[\s.\-–—]|$)\s*[.\-–—]?\s*(.*)$")
RE_PAR = re.compile(r"^§\s*(\d+)\s*[º°o]?\s*" + SUF + r"(?=[\s.\-–—]|$)\s*[.\-–—]?\s*(.*)$")
RE_PUN = re.compile(r"^Par[áa]grafo\s+[úu]nico\s*[.\-–—]?\s*(.*)$", re.I)
RE_INC = re.compile(r"^(M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))(?<=[IVXLCDM])" + SUF + r"\s*[-–—]\s*(.*)$")
RE_ALI = re.compile(r"^([a-z]{1,2})\)\s*(.*)$")
RE_ITEM = re.compile(r"^(\d+)\s*[.)\-–—]\s+(.*)$")
NIVEIS = ["parte", "livro", "titulo", "capitulo", "secao", "subsecao"]
RE_HEAD = re.compile(
    r"^(PARTE|LIVRO|T[ÍI]TULO|CAP[ÍI]TULO|SUBSE[ÇC][ÃA]O|SE[ÇC][ÃA]O)\s+"
    r"([IVXLCDM]+(?:-[A-Z])?|[ÚU]NIC[OA]|GERAL|ESPECIAL|PRIMEIR[OA]|SEGUND[OA]|TERCEIR[OA]|\d+)\b[\s.\-–—]*(.*)$", re.I)
RE_FIM = re.compile(r"^(Bras[íi]lia|Rio de Janeiro|Senado Federal|Congresso Nacional|Pal[áa]cio)\b.{0,40}\d{1,2}.{0,25}\d{4}", re.I)
RE_ANEXO = re.compile(r"^ANEXO\b")
ABRE_ASPAS = "“\"‘'«"
FECHA_ASPAS = re.compile(r"[”\"’'»]\s*(\(NR\))?\s*[.;]?\s*(\([^)]*\)\s*)*$")
RE_REVOGADO = re.compile(r"\(\s*Revogad[oa]", re.I)
CABECALHO = {"câmara dos deputados", "centro de documentação e informação", "senado federal",
             "secretaria-geral da mesa", "secretaria de informação legislativa",
             "presidência da república", "casa civil", "subchefia para assuntos jurídicos"}


def _nivel(palavra):
    p = palavra.lower().translate(str.maketrans("íçã", "ica"))
    return NIVEIS.index(p)


def _linha(d):
    if d["tipo"] == "caput":
        return d["texto"]
    sep = " -" if d["tipo"] == "inciso" else ""
    return f'{d["rotulo"]}{sep} {d["texto"]}'.strip()


def parse(paragrafos):
    """[(texto, tachado)] -> {"preambulo", "artigos", "rodape", "texto_integral"}.

    Cada artigo: {"numero", "rotulo", "rubrica", "caminho", "texto", "revogado", "dispositivos"};
    cada dispositivo: {"tipo", "rotulo", "texto", "revogado"} na ordem do texto (o caput é o primeiro).
    """
    paragrafos = [(t, r) for t, r in paragrafos
                  if t.lower() not in CABECALHO and not re.fullmatch(r"###.*@@@", t)]  # marcador interno da fonte
    preambulo, rodape, artigos = [], [], []
    caminho = {}        # nível -> rótulo do agrupamento aberto
    titulo_pendente = None  # nível cujo título (linha seguinte) ainda não chegou
    em_aspas = fim = False
    saldo = 0           # aspas curvas abertas e ainda não fechadas no bloco citado
    atual = None        # último dispositivo, que recebe as linhas de continuação

    def novo(tipo, rotulo, texto, riscado):
        nonlocal atual
        atual = {"tipo": tipo, "rotulo": rotulo, "texto": texto,
                 "revogado": riscado or bool(RE_REVOGADO.search(texto))}
        artigos[-1]["dispositivos"].append(atual)

    rubrica = None      # epígrafe do próximo artigo ("Homicídio simples"), comum nos códigos
    for i, (texto, riscado) in enumerate(paragrafos):
        if fim:
            rodape.append(texto)
            continue
        # texto entre aspas é a redação que uma lei alteradora dá a OUTRA lei: nunca vira estrutura
        if em_aspas and RE_FIM.match(texto):  # aspas nunca fechadas não podem engolir o resto da norma
            em_aspas = False
        if em_aspas or texto[0] in ABRE_ASPAS:
            if not em_aspas:
                saldo = 0
            saldo += texto.count("“") - texto.count("”")
            # com aspas curvas vale o saldo (um termo “citado” dentro do bloco não o fecha)
            em_aspas = saldo > 0 if "“" in texto or "”" in texto or saldo else not FECHA_ASPAS.search(texto[1:])
            if atual:
                atual["texto"] += "\n" + texto
            else:
                preambulo.append(texto)
            continue
        if m := RE_HEAD.match(texto):
            n = _nivel(m[1])
            caminho = {k: v for k, v in caminho.items() if k < n}
            caminho[n] = f"{m[1].capitalize()} {m[2].upper()}"
            titulo_pendente = n
            if m[3]:
                caminho[n] += " - " + m[3]
                titulo_pendente = None
            atual = None
            continue
        if m := RE_ART.match(texto):
            num = m[1].replace(".", "")
            numero = num + ("º" if int(num) < 10 else "") + (f"-{m[2]}" if m[2] else "")
            artigos.append({"numero": numero, "rotulo": f"Art. {numero}",
                            "rubrica": rubrica, "caminho": [caminho[k] for k in sorted(caminho)],
                            "texto": "", "revogado": False, "dispositivos": []})
            novo("caput", f"Art. {numero}", m[3], riscado)
            titulo_pendente = rubrica = None
            continue
        if titulo_pendente is not None:  # linha logo após "CAPÍTULO I" é o nome do capítulo
            if not texto.startswith("("):  # anotação "(Redação dada pela...)" não é nome
                caminho[titulo_pendente] += " - " + texto
                titulo_pendente = None
            continue
        seguinte = paragrafos[i + 1][0] if i + 1 < len(paragrafos) else ""
        dispositivo = RE_PAR.match(texto) or RE_PUN.match(texto) or RE_INC.match(texto) or RE_ALI.match(texto)
        if not dispositivo and len(texto) < 150 and texto[-1] not in ".;:,)" and RE_ART.match(seguinte):
            rubrica = texto
            continue
        if not artigos:
            preambulo.append(texto)
            continue
        if RE_FIM.match(texto) or RE_ANEXO.match(texto):
            fim = True
            rodape.append(texto)
        elif m := RE_PAR.match(texto):
            novo("paragrafo", f"§ {m[1]}º" + (f"-{m[2]}" if m[2] else ""), m[3], riscado)
        elif m := RE_PUN.match(texto):
            novo("paragrafo", "Parágrafo único", m[1], riscado)
        elif m := RE_INC.match(texto):
            novo("inciso", m[1] + (f"-{m[2]}" if m[2] else ""), m[3], riscado)
        elif m := RE_ALI.match(texto):
            novo("alinea", m[1] + ")", m[2], riscado)
        elif (m := RE_ITEM.match(texto)) and atual and atual["tipo"] in ("alinea", "item"):
            novo("item", m[1] + ".", m[2], riscado)
        elif atual:
            atual["texto"] += "\n" + texto

    for a in artigos:
        d = a["dispositivos"]
        ponto = "" if "º" in a["numero"] else "."
        a["texto"] = f'{a["rotulo"]}{ponto} ' + "\n".join(map(_linha, d))
        a["revogado"] = d[0]["revogado"]
    return {"preambulo": "\n".join(preambulo), "artigos": artigos, "rodape": "\n".join(rodape),
            "texto_integral": "\n".join(t for t, _ in paragrafos)}


def render_md(doc):
    plano = lambda t: " ".join((t or "").split())  # texto remoto não pode quebrar o bloco Markdown
    out = [f'# {plano(doc["nome"])}', ""]
    if doc.get("ementa"):
        out += [f'> {plano(doc["ementa"])}', ""]
    out += [f'*Fonte: <{doc["url"]}> · versão do texto: {doc["versao"] or "sem texto"} · licença CC-BY 4.0*', ""]
    if not doc["artigos"]:
        return "\n".join(out + [doc["texto_integral"].replace("\n", "\n\n"), ""])
    if doc["preambulo"]:
        out += [doc["preambulo"].replace("\n", "\n\n"), ""]
    anterior = []
    recuo = {"paragrafo": 0, "inciso": 1, "alinea": 2, "item": 3}
    for a in doc["artigos"]:
        n = len(os.path.commonprefix([anterior, a["caminho"]]))  # só imprime os títulos que mudaram
        for i, rot in enumerate(a["caminho"][n:], n):
            out += [f'{"#" * min(6, i + 2)} {rot}', ""]
        anterior = a["caminho"]
        for d in a["dispositivos"]:
            t = d["texto"].replace("\n", " ")
            if d["revogado"] and t:
                t = f"~~{t}~~"
            if d["tipo"] == "caput":
                if a["rubrica"]:
                    out += [f'*{a["rubrica"]}*', ""]
                out += [f'**{d["rotulo"]}** {t}'.rstrip(), ""]
            else:
                sep = " -" if d["tipo"] == "inciso" else ""
                out.append(f'{"  " * recuo[d["tipo"]]}- {d["rotulo"]}{sep} {t}'.rstrip())
        if len(a["dispositivos"]) > 1:
            out.append("")
    if doc["rodape"]:
        out += ["---", "", doc["rodape"].replace("\n", "  \n"), ""]
    return "\n".join(out)


# ---------------------------------------------------------------- pipeline

def listar(ano):
    try:
        d = get_json(f"{SENADO}/lista?ano={ano}") or {}
    except NotFound:  # ano sem normas
        return []
    docs = ((d.get("ListaDocumento") or {}).get("documentos") or {}).get("documento") or []
    return docs if isinstance(docs, list) else [docs]


def escolher_versoes(meta):
    """Textos disponíveis, do preferido ao último recurso: compilado, original, intermediário.

    "Intermediate" costuma ser só uma retificação (errata), não o texto inteiro.
    """
    ordem = {"current": 0, "original": 1, "intermediate": 2}
    enc = meta.get("encoding") or []
    enc = [e for e in ([enc] if isinstance(enc, dict) else enc)
           if isinstance(e, dict) and e.get("contentUrl") and "Traduzid" not in (e.get("name") or "")]
    enc.sort(key=lambda e: e.get("datePublished") or "", reverse=True)
    return sorted(enc, key=lambda e: ordem.get(str(e.get("version")).lower(), 3))


def _iso(data_br):
    try:
        return datetime.datetime.strptime(data_br, "%d/%m/%Y").date().isoformat()
    except (TypeError, ValueError):
        return None


def caminho_saida(out, sub, row, ext):
    seguro = lambda s: re.sub(r"[^A-Za-z0-9._-]", "_", s).lstrip(".") or "_"  # texto cru da API nunca vira caminho (nem "..")
    nome = seguro(row.get("norma") or row["id"])
    return os.path.join(out, sub, seguro(row.get("tipo") or "outros"), seguro(str(row.get("anoassinatura") or "0")),
                        f"{nome}.{ext}")


def _gravar(path, texto):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, path)


def _url_oficial(url):
    """URLs vindas da API só são seguidas/publicadas se forem https do portal oficial."""
    u = urllib.parse.urlparse(url)
    return u.scheme == "https" and u.hostname == "normas.leg.br"


def processar(row, out):
    """Baixa e converte uma norma. Devolve 'done' (com texto) ou 'semtexto' (só metadados)."""
    det = get_json(f'{SENADO}/{urllib.parse.quote(str(row["id"]), safe="")}') or {}
    docs = ((det.get("DetalheDocumento") or {}).get("documentos") or {}).get("documento") or [{}]
    ident = (docs[0] if isinstance(docs, list) else docs).get("identificacao") or {}
    url = ident.get("urlDocumento") or ""
    if not _url_oficial(url):
        url = ""
    urn = (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("urn") or [""])[0]
    meta, versao, corpo = {}, None, {"preambulo": "", "artigos": [], "rodape": "", "texto_integral": ""}
    fonte = None
    if urn:
        try:
            meta = get_json(f"{NORMAS}/normas?urn={urllib.parse.quote(urn, safe=':;')}&tipo_documento=maior-detalhe") or {}
        except NotFound:
            pass
        if isinstance(meta, list):
            meta = meta[0] if meta else {}
        for v in escolher_versoes(meta):
            # o contentUrl publicado omite o "/public" e dá 404
            url_texto = v["contentUrl"].replace("/api/binario/", "/api/public/binario/")
            if not _url_oficial(url_texto):
                continue
            try:
                c = parse(extrair_paragrafos(get(url_texto, accept="*/*").decode("utf-8", "replace")))
            except NotFound:
                continue
            if c["texto_integral"]:
                corpo, versao, fonte = c, v, url_texto
                break
    doc = {
        "id": row["id"], "tipo": row.get("tipo"), "tipo_descricao": row.get("descricao"),
        "numero": row.get("numero"), "ano": int(row.get("anoassinatura") or 0) or None,
        "norma": row.get("norma"), "nome": meta.get("headline") or row.get("normaNome"),
        "ementa": row.get("ementa") or meta.get("abstract"), "data": _iso(row.get("dataassinatura")),
        "apelidos": [a for a in meta.get("alternateName") or [] if a != row.get("norma")],
        "urn": urn or None, "url": url or f'https://legis.senado.leg.br/norma/{row["id"]}',
        "palavras_chave": meta.get("keywords") or [],
        "versao": {"current": "compilada", "intermediate": "intermediaria"}.get(
            str(versao.get("version")).lower(), "original") if versao else None,
        "versao_descricao": versao.get("name") if versao else None,
        "fonte_texto": fonte, "licenca": "CC-BY-4.0",
        "baixado_em": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        **corpo,
    }
    _gravar(caminho_saida(out, "json", row, "json"), json.dumps(doc, ensure_ascii=False, indent=1))
    _gravar(caminho_saida(out, "md", row, "md"), render_md(doc))
    return "done" if versao else "semtexto"


def main(argv=None):
    global RPS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data", help="pasta de saída (padrão: data)")
    ap.add_argument("--tipos", default="", help="siglas separadas por vírgula, ex.: LEI,LCP,DEC,DEL,MPV,EMC,CON "
                    "(padrão: todos os tipos federais)")
    ap.add_argument("--ano-ini", type=int, default=1808)
    ap.add_argument("--ano-fim", type=int, default=datetime.date.today().year)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rps", type=float, default=RPS, help="máximo de requisições por segundo (padrão: 4)")
    ap.add_argument("--limit", type=int, help="processa no máximo N normas (para testar)")
    ap.add_argument("--retry-failed", action="store_true", help="tenta de novo as normas que falharam")
    a = ap.parse_args(argv)
    if a.rps <= 0 or a.workers < 1:
        ap.error("--rps e --workers devem ser positivos")
    RPS = a.rps
    tipos = [t.strip().upper() for t in a.tipos.split(",") if t.strip()]

    os.makedirs(a.out, exist_ok=True)
    db = sqlite3.connect(os.path.join(a.out, "state.sqlite"))
    db.executescript("""
        CREATE TABLE IF NOT EXISTS norms(id TEXT PRIMARY KEY, tipo TEXT, ano INT, raw TEXT,
                                         status TEXT DEFAULT 'pending', err TEXT);
        CREATE TABLE IF NOT EXISTS listed(ano INT PRIMARY KEY);
    """)

    # fase 1: listar (uma chamada por ano traz todos os tipos)
    feitos = {r[0] for r in db.execute("SELECT ano FROM listed")}
    este_ano = datetime.date.today().year
    for ano in range(a.ano_ini, a.ano_fim + 1):
        if ano in feitos and ano < este_ano:  # o ano corrente sempre é relistado: ganha normas novas
            continue
        docs = listar(ano)
        with db:
            db.executemany("INSERT OR IGNORE INTO norms(id, tipo, ano, raw) VALUES(?,?,?,?)",
                           [(d["id"], d.get("tipo"), ano, json.dumps(d, ensure_ascii=False)) for d in docs if d.get("id")])
            db.execute("INSERT OR IGNORE INTO listed VALUES(?)", (ano,))
        print(f"listado {ano}: {len(docs)} normas", file=sys.stderr)

    # fase 2: baixar e converter
    status = ["pending"] + (["failed"] if a.retry_failed else [])
    sql = f"SELECT raw FROM norms WHERE ano BETWEEN ? AND ? AND status IN ({','.join('?' * len(status))})"
    args = [a.ano_ini, a.ano_fim, *status]
    if tipos:  # "LEI" casa com LEI, LEI-n, LEI-sn
        sql += " AND (" + " OR ".join("upper(tipo) = ? OR upper(tipo) LIKE ?" for _ in tipos) + ")"
        args += [x for t in tipos for x in (t, t + "-%")]
    sql += " ORDER BY ano DESC, id"
    if a.limit:
        sql += f" LIMIT {int(a.limit)}"
    rows = [json.loads(r[0]) for r in db.execute(sql, args)]
    print(f"{len(rows)} normas a processar", file=sys.stderr)

    def tarefa(row):
        try:
            return row["id"], processar(row, a.out), None
        except Exception as e:  # uma norma ruim não derruba a coleta; fica 'failed' para --retry-failed
            return row["id"], "failed", repr(e)[:300]

    cont = {}
    ex = ThreadPoolExecutor(a.workers)
    try:
        for i, (id_, st, err) in enumerate(ex.map(tarefa, rows), 1):
            db.execute("UPDATE norms SET status=?, err=? WHERE id=?", (st, err, id_))
            cont[st] = cont.get(st, 0) + 1
            if i % 50 == 0:
                db.commit()
                print(f"{i}/{len(rows)} {cont}", file=sys.stderr)
    except KeyboardInterrupt:
        print("interrompido; rode de novo para continuar", file=sys.stderr)
    finally:
        ex.shutdown(wait=True, cancel_futures=True)  # sem isso o Ctrl-C esperaria a fila inteira
        db.commit()
        db.close()
    print(f"fim: {cont}", file=sys.stderr)
    return 1 if cont.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
