"""Apoio dos testes do boletim (E08B): relógio fixo, HTTP falso e feeds
fictícios. Módulo com nome próprio para não colidir com outros conftest."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

import httpx

import noticias as nt

UTC = timezone.utc
TZ = nt.TZ_BRASILIA


def em_brasilia(ano, mes, dia, hora=0, minuto=0) -> datetime:
    """Instante a partir do relógio de parede de Brasília, devolvido em UTC."""
    return datetime(ano, mes, dia, hora, minuto, tzinfo=TZ).astimezone(UTC)


class Relogio:
    def __init__(self, instante: datetime):
        self.instante = instante

    def __call__(self) -> datetime:
        return self.instante

    def avancar(self, **kw):
        self.instante += timedelta(**kw)
        return self.instante


class HttpFalso:
    """Servidor HTTP falso. Cada URL tem uma resposta (ou uma exceção) e as
    chamadas são contadas: é como os testes provam quem buscou o quê."""

    def __init__(self):
        self.rotas: dict[str, object] = {}
        self.chamadas: list[str] = []

    def responder(self, url, corpo="", status=200, tipo="application/rss+xml", headers=None):
        h = {"content-type": tipo, **(headers or {})}
        self.rotas[url] = (status, h, corpo.encode() if isinstance(corpo, str) else corpo)

    def falhar(self, url, excecao):
        self.rotas[url] = excecao

    def _servir(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.chamadas.append(url)
        r = self.rotas.get(url)
        if r is None:
            raise AssertionError(f"pedido HTTP não cadastrado no teste: {url}")
        if isinstance(r, Exception):
            raise r
        status, headers, corpo = r
        return httpx.Response(status, headers=headers, content=corpo)

    @property
    def transporte(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._servir)

    def quantas(self, url) -> int:
        return sum(1 for c in self.chamadas if c == url)


# ── Feeds fictícios (estrutura dos reais) ─────────────────────────────────────

def rss2(itens: list[dict]) -> str:
    """RSS 2.0 no formato do WordPress do CFP (pubDate RFC 822, description
    com o rodapé "O post … apareceu primeiro em …")."""
    partes = []
    for it in itens:
        data = f"<pubDate>{format_datetime(it['data'])}</pubDate>" if it.get("data") else ""
        desc = it.get("desc", "Texto fictício de teste.")
        rodape = (f'<p>O post <a rel="nofollow" href="{escape(it["link"])}">{escape(it["titulo"])}</a> '
                  f'apareceu primeiro em <a rel="nofollow" href="https://site.cfp.org.br">Conselho Federal de Psicologia</a>.</p>')
        partes.append(f"""<item><title>{escape(it['titulo'])}</title><link>{escape(it['link'])}</link>
<dc:creator><![CDATA[Fulano Fictício]]></dc:creator>{data}<category><![CDATA[Geral]]></category>
<guid isPermaLink="false">https://site.cfp.org.br/?p={abs(hash(it['link'])) % 10**6}</guid>
<description><![CDATA[<p>{desc}</p>
{rodape}]]></description></item>""")
    return ("""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Conselho Federal de Psicologia</title><link>https://site.cfp.org.br</link>"""
            + "".join(partes) + "</channel></rss>")


def atom(entradas: list[dict]) -> str:
    """Atom no formato do Plone do gov.br (published ≠ updated; <author> com
    um número de 11 dígitos, que o adaptador NÃO deve ler)."""
    partes = []
    for e in entradas:
        pub = f"<published>{e['data'].isoformat()}</published>" if e.get("data") else ""
        upd = e.get("atualizado", e.get("data"))
        partes.append(f"""<entry><title>{escape(e['titulo'])}</title>
<link rel="alternate" type="text/html" href="{escape(e['link'])}"/>
<id>urn:syndication:{abs(hash(e['link'])) % 10**8}</id><summary>{escape(e.get('desc', 'Resumo fictício.'))}</summary>
<author><name>00000000000</name></author>{pub}<updated>{upd.isoformat() if upd else ''}</updated></entry>""")
    return ('<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            "<title>Notícias da Saúde</title>" + "".join(partes) + "</feed>")


URL_CFP = nt.CFP.url
URL_MS = nt.MINISTERIO_SAUDE.url


def link_cfp(n) -> str:
    return f"https://site.cfp.org.br/noticia-ficticia-{n}/"


def link_ms(n) -> str:
    return f"https://www.gov.br/saude/pt-br/assuntos/noticias-ms/2026/outubro/noticia-ficticia-{n}"
