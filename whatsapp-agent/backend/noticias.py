"""Boletim de notícias por segmento (E08B) — fontes, coleta, normalização e seleção.

Funções puras e determinísticas; o agendamento, o cache e a leitura pelo painel
ficam em `noticias_job.py`.

Regras que este módulo garante:

- **Só fontes da allowlist**, cada uma conferida por pessoa (`verificada_em`).
  O endereço do feed é o que a própria fonte anuncia no HTML (`<link
  rel="alternate">`); nada de RSS ou API presumidos.
- **HTTP defensivo:** só https, timeout, tamanho máximo, redirect só para os
  hosts da própria fonte, nenhum JavaScript executado, nenhuma URL vinda de
  usuário ou de artigo é buscada.
- **Conteúdo externo é dado não confiável.** Título e resumo viram texto puro
  (sem HTML), com tamanho limitado; links só https e do host da fonte. Texto
  de artigo nunca é tratado como instrução.
- **Seleção sem IA:** janela de 7 dias pela data de **publicação** (não pela
  data de coleta nem pela de atualização), prioridade por termos verificáveis,
  deduplicação por URL e por tema. Dado de autor do feed não é lido.
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")
UTC = timezone.utc

JANELA = timedelta(days=7)
TOLERANCIA_FUTURO = timedelta(minutes=10)   # relógio da fonte um pouco adiantado
MAX_DESTAQUES = 5
DESTAQUES_VISIVEIS = 3                      # o painel mostra 3 e oferece "mais"

MAX_BYTES = 2_000_000
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_REDIRECTS = 3
MAX_TITULO = 180
MAX_RESUMO = 240
USER_AGENT = "AgenteConsultorio-Boletim/1.0 (leitura diaria de noticias publicas)"
TIPOS_ACEITOS = {
    "application/rss+xml", "application/atom+xml", "application/xml",
    "text/xml", "application/rdf+xml",
}


# ── Fontes e segmentos ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fonte:
    id: str
    nome: str
    url: str                      # feed anunciado pela própria fonte
    formato: str                  # "rss2" | "atom"
    hosts: frozenset              # hosts aceitos para o feed, redirects e links
    exige_tema: bool = False      # fonte geral: só entra item de saúde mental/psicologia
    verificada_em: str = ""       # AAAA-MM-DD em que uma pessoa conferiu o feed
    ficticia: bool = False        # só prévia/teste; nunca aparece como notícia real


@dataclass(frozen=True)
class Segmento:
    id: str
    titulo: str
    fontes: tuple


CFP = Fonte(
    id="cfp",
    nome="Conselho Federal de Psicologia",
    # Anunciado em https://site.cfp.org.br/noticias/ (<link rel="alternate"
    # type="application/rss+xml">). RSS 2.0 do WordPress, com pubDate.
    url="https://site.cfp.org.br/feed/",
    formato="rss2",
    hosts=frozenset({"site.cfp.org.br"}),
    verificada_em="2026-10-20",
)

MINISTERIO_SAUDE = Fonte(
    id="ms-saude",
    nome="Ministério da Saúde",
    # Anunciado em https://www.gov.br/saude/pt-br/assuntos/noticias-ms (Plone).
    # Atom, e não o RSS 2.0 da mesma pasta: o Atom traz <link> do artigo e
    # separa <published> de <updated>; o RSS 2.0 não tem <link> por item.
    url="https://www.gov.br/saude/pt-br/assuntos/noticias-ms/noticias-da-saude/atom.xml",
    formato="atom",
    hosts=frozenset({"www.gov.br"}),
    exige_tema=True,       # feed geral de saúde: só itens de saúde mental/psicologia
    verificada_em="2026-10-20",
)

SEGMENTOS = {
    "psicologia": Segmento("psicologia", "Psicologia em foco", (CFP, MINISTERIO_SAUDE)),
}

# Mapeamento EXPLÍCITO de tenant.segment para o boletim. Segmento ausente daqui
# = módulo desligado. De propósito NÃO usa segments.normalize_segment, que
# devolve 'psicologia' para valor vazio ou desconhecido: aqui um segmento
# desconhecido não recebe notícias de psicologia em silêncio.
MAPA_SEGMENTO = {"psicologia": "psicologia"}


def modulo_habilitado() -> bool:
    """Chave geral (NOTICIAS_HABILITADO=0 desliga painel e job; o painel volta
    ao comportamento anterior). Padrão: ligado."""
    return (os.getenv("NOTICIAS_HABILITADO") or "1").strip().lower() not in ("0", "false", "no", "nao")


def segmento_do_tenant(tenant: dict | None) -> str | None:
    if not modulo_habilitado() or not tenant:
        return None
    bruto = tenant.get("segment")
    if not isinstance(bruto, str):
        return None
    return MAPA_SEGMENTO.get(bruto.strip().lower())


# ── Item normalizado ──────────────────────────────────────────────────────────

@dataclass
class Item:
    fonte_id: str
    url: str                      # link exibido (https, host da fonte, sem rastreio)
    chave: str                    # chave de deduplicação da URL
    titulo: str
    resumo: str
    publicado_em: datetime | None  # aware (UTC); None = fonte não informou
    atualizado_em: datetime | None = None
    categorias: tuple = ()
    ficticio: bool = False
    pontos: int = 0
    motivos: tuple = field(default_factory=tuple)


class ErroColeta(Exception):
    """Falha ao obter ou interpretar um feed. A mensagem vai para o log e para o
    estado da fonte; nunca contém conteúdo do feed."""


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _host_permitido(url: str, fonte: Fonte) -> bool:
    p = urlsplit(url)
    return (p.scheme == "https" and (p.hostname or "").lower() in fonte.hosts
            and not p.username and not p.password and p.port in (None, 443))


def baixar(fonte: Fonte, cliente: httpx.Client) -> bytes:
    """GET do feed com as travas acima. Levanta ErroColeta em qualquer desvio."""
    url = fonte.url
    for _ in range(MAX_REDIRECTS + 1):
        if not _host_permitido(url, fonte):
            raise ErroColeta("endereço fora da allowlist da fonte")
        try:
            with cliente.stream("GET", url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
            }) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code != 200:
                    raise ErroColeta(f"HTTP {r.status_code}")
                tipo = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if tipo not in TIPOS_ACEITOS:
                    raise ErroColeta(f"tipo de conteúdo inesperado: {tipo or 'vazio'}")
                declarado = r.headers.get("content-length", "")
                if declarado.isdigit() and int(declarado) > MAX_BYTES:
                    raise ErroColeta("resposta maior que o limite")
                corpo = bytearray()
                for pedaco in r.iter_bytes():
                    corpo += pedaco
                    if len(corpo) > MAX_BYTES:
                        raise ErroColeta("resposta maior que o limite")
                return bytes(corpo)
        except httpx.TimeoutException:
            raise ErroColeta("tempo esgotado")
        except httpx.HTTPError as e:
            raise ErroColeta(f"falha de rede: {type(e).__name__}")
    raise ErroColeta("redirects demais")


def novo_cliente(transporte: httpx.BaseTransport | None = None) -> httpx.Client:
    # follow_redirects=False: cada redirect passa pela allowlist em baixar().
    # trust_env=False: proxy/netrc do ambiente não interferem.
    return httpx.Client(transport=transporte, timeout=TIMEOUT,
                        follow_redirects=False, trust_env=False)


# ── Texto e URL ───────────────────────────────────────────────────────────────

class _SoTexto(HTMLParser):
    """Extrai só o texto; descarta conteúdo de <script>/<style>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self._ignorar = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._ignorar += 1
        elif tag in ("p", "br", "li", "div"):
            self.partes.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._ignorar:
            self._ignorar -= 1

    def handle_data(self, data):
        if not self._ignorar:
            self.partes.append(data)


def texto_puro(valor: str | None, limite: int) -> str:
    if not valor:
        return ""
    p = _SoTexto()
    try:
        p.feed(valor)
        p.close()
        txt = "".join(p.partes)
    except Exception:
        txt = re.sub(r"<[^>]*>", " ", valor)
    txt = html.unescape(txt)
    # caracteres de controle e de formatação invisível (inclui bidi) viram espaço
    txt = "".join(" " if unicodedata.category(c) in ("Cc", "Cf") else c for c in txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) <= limite:
        return txt
    corte = txt[: limite - 1].rsplit(" ", 1)[0].rstrip(" ,.;:-–—")
    return corte + "…"


_RODAPE_WP = re.compile(r"\s*(O post|The post)\b.*?\b(apareceu primeiro em|appeared first on)\b.*$", re.S | re.I)


def _resumo(valor: str | None) -> str:
    txt = texto_puro(valor, 4000)
    txt = _RODAPE_WP.sub("", txt).strip()
    txt = re.sub(r"\s*\[(…|\.\.\.)\]\s*$", "…", txt)
    return texto_puro(txt, MAX_RESUMO)


_PARAMS_RASTREIO = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$)", re.I)


def url_publica(url: str | None, fonte: Fonte) -> tuple[str, str] | None:
    """(link exibido, chave de dedup) ou None se o link não for da fonte/https."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not _host_permitido(url, fonte):
        return None
    p = urlsplit(url)
    consulta = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                if not _PARAMS_RASTREIO.match(k)]
    host = (p.hostname or "").lower()
    caminho = p.path or "/"
    link = urlunsplit(("https", host, caminho, urlencode(consulta), ""))
    chave = urlunsplit(("https", host, caminho.rstrip("/") or "/", urlencode(sorted(consulta)), ""))
    return link, chave


def _data_rfc822(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        d = email.utils.parsedate_to_datetime(valor.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if d is None:
        return None
    if d.tzinfo is None:          # "-0000" no RFC 2822: fuso não informado; trata como UTC
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def _data_iso(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        d = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:          # data sem fuso é ambígua: não vira "desta semana"
        return None
    return d.astimezone(UTC)


# ── Interpretação dos feeds ───────────────────────────────────────────────────

_ATOM = "{http://www.w3.org/2005/Atom}"
_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


def interpretar(fonte: Fonte, conteudo: bytes) -> tuple[list[Item], dict]:
    """Converte o feed em itens normalizados. Retorna (itens, descartes)."""
    if _DTD.search(conteudo):
        # Sem DTD não há expansão de entidades (bilhão de risadas, XXE).
        raise ErroColeta("feed com DTD/ENTITY recusado")
    try:
        raiz = ET.fromstring(conteudo)
    except ET.ParseError:
        raise ErroColeta("XML inválido")
    descartes: dict[str, int] = {}

    def descarta(motivo: str):
        descartes[motivo] = descartes.get(motivo, 0) + 1

    brutos = []
    if fonte.formato == "rss2" and raiz.tag == "rss":
        for it in raiz.findall("./channel/item"):
            link = (it.findtext("link") or "").strip()
            if not link:
                guid = it.find("guid")
                if guid is not None and (guid.get("isPermaLink") or "true").lower() != "false":
                    link = (guid.text or "").strip()
            brutos.append((it.findtext("title"), link, _data_rfc822(it.findtext("pubDate")), None,
                           it.findtext("description"),
                           tuple((c.text or "").strip().lower() for c in it.findall("category") if c.text)))
    elif fonte.formato == "atom" and raiz.tag == _ATOM + "feed":
        for e in raiz.findall(_ATOM + "entry"):
            link = ""
            for l in e.findall(_ATOM + "link"):
                if (l.get("rel") or "alternate") == "alternate":
                    link = (l.get("href") or "").strip()
                    break
            # <author> não é lido: no feed do gov.br ele traz um número de 11
            # dígitos no formato de CPF.
            brutos.append((e.findtext(_ATOM + "title"), link,
                           _data_iso(e.findtext(_ATOM + "published")),
                           _data_iso(e.findtext(_ATOM + "updated")),
                           e.findtext(_ATOM + "summary") or e.findtext(_ATOM + "content"),
                           tuple((c.get("term") or "").strip().lower() for c in e.findall(_ATOM + "category") if c.get("term"))))
    else:
        raise ErroColeta(f"formato inesperado para {fonte.formato}")

    itens: list[Item] = []
    for titulo, link, publicado, atualizado, descricao, categorias in brutos:
        t = texto_puro(titulo, MAX_TITULO)
        if not t:
            descarta("sem título")
            continue
        u = url_publica(link, fonte)
        if not u:
            descarta("link ausente ou fora da fonte")
            continue
        itens.append(Item(fonte_id=fonte.id, url=u[0], chave=u[1], titulo=t,
                          resumo=_resumo(descricao), publicado_em=publicado,
                          atualizado_em=atualizado, categorias=categorias,
                          ficticio=fonte.ficticia))
    return itens, descartes


# ── Seleção ───────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


# Termos verificáveis (texto sem acento, minúsculo). Cada grupo conta uma vez.
_NORMATIVO = re.compile(
    r"\bresolucao\b|\bnota tecnica\b|\bnota publica\b|\borienta|\bregulament|\bnormativ"
    r"|\bcodigo de etica\b|\bconsulta publica\b|\bprazos?\b|\bprorroga|\binscricoes\b"
    r"|\bedital\b|\blei\b|\bdecreto\b|\bportaria\b|\bmedida provisoria\b"
    r"|\breferencias tecnicas\b|\bfiscaliza")
_SAUDE_MENTAL = re.compile(
    r"\bsaude mental\b|\bpsicolog|\bpsiquiatr|\bsuicid|\bansiedade\b|\bdepress"
    r"|\bcaps\b|\braps\b|\bsofrimento psiquico\b|\bsetembro amarelo\b|\bjaneiro branco\b"
    r"|\bautomutila|\bautolesao\b|\balcool e outras drogas\b|\bdependencia quimica\b")
_INSTITUCIONAL = re.compile(
    r"\bnota de sentir\b|\bnota de pesar\b|\bfalecimento\b|\bhomenage|\bposse\b"
    r"|\bsolenidade\b|\bvisita\b|\breuniao\b|\bencontros? de\b|\bplanejamento estrategico\b"
    r"|\bparticipa (de|do|da)\b|\baniversario\b|\bcompleta \d+ anos\b")

_VAZIAS = set("""para pela pelo pelos pelas como sobre entre desde ate apos este esta estes estas
essa esse isso aqui mais menos muito muita seus suas sua seu nossa nosso quando onde
qual quais pode podem deve devem sera serao foram sendo todas todos toda todo cada
com sem que dos das nos nas uma uns umas""".split())


def _tokens(s: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", _norm(s)) if len(t) >= 4 and t not in _VAZIAS}


def pontuar(item: Item, fonte: Fonte) -> tuple[int, tuple]:
    texto = _norm(f"{item.titulo} {item.resumo}")
    pontos, motivos = 0, []
    if _NORMATIVO.search(texto):
        pontos += 3
        motivos.append("normativo/prazo")
    tema = bool(_SAUDE_MENTAL.search(texto))
    if tema:
        pontos += 2
        motivos.append("saúde mental/psicologia")
    if _INSTITUCIONAL.search(_norm(item.titulo)):
        pontos -= 4
        motivos.append("agenda institucional")
    if fonte.exige_tema and not tema:
        return -100, ("fonte geral sem tema de saúde mental",)
    return pontos, tuple(motivos)


def selecionar(itens: list[Item], fontes: dict, agora: datetime,
               maximo: int = MAX_DESTAQUES) -> tuple[list[Item], dict]:
    """Destaques da semana. Nunca completa a lista com item fraco: menos de 3
    itens relevantes = menos de 3 destaques; nenhum = estado vazio."""
    agora = agora.astimezone(UTC)
    inicio, limite = agora - JANELA, agora + TOLERANCIA_FUTURO
    descartes: dict[str, int] = {}

    def descarta(motivo):
        descartes[motivo] = descartes.get(motivo, 0) + 1

    candidatos, chaves = [], set()
    for it in itens:
        f = fontes.get(it.fonte_id)
        if f is None:
            descarta("fonte fora do segmento")
            continue
        if it.chave in chaves:
            descarta("URL repetida")
            continue
        chaves.add(it.chave)
        if it.publicado_em is None:
            descarta("sem data de publicação")
            continue
        if it.publicado_em > limite:
            descarta("data de publicação no futuro")
            continue
        if it.publicado_em < inicio:
            descarta("publicado há mais de 7 dias")
            continue
        it.pontos, it.motivos = pontuar(it, f)
        if it.pontos < 0:
            descarta("fora do tema" if it.pontos == -100 else "agenda institucional")
            continue
        candidatos.append(it)

    candidatos.sort(key=lambda i: (i.pontos, i.publicado_em), reverse=True)
    escolhidos: list[Item] = []
    for it in candidatos:
        tk = _tokens(it.titulo)
        if any(_jaccard(tk, _tokens(e.titulo)) >= 0.6 for e in escolhidos):
            descarta("mesmo tema de outro destaque")
            continue
        escolhidos.append(it)
        if len(escolhidos) >= maximo:
            break
    return escolhidos, descartes


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def assinatura(itens: list[Item]) -> str:
    """Identidade da seleção: muda só quando o conteúdo exibido muda."""
    base = "\n".join(f"{i.chave}|{i.titulo}|{i.resumo}" for i in itens)
    return hashlib.sha256(base.encode()).hexdigest()
