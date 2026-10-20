"""Procura alegações retiradas no TEXTO VISÍVEL das páginas de venda.

Remove comentários, <script>, <style> e tags antes de buscar, para que número e
frase em tags separadas ("40%" + "Menos faltas") sejam vistos juntos, como o
visitante vê. Sai com 1 se encontrar qualquer ocorrência.

Uso: python3 alegacoes_texto.py <base-url>
"""
import html
import re
import sys
import urllib.request

PAGINAS = ["/", "/p/psicologia", "/p/nutricao", "/p/odontologia"]
PADROES = [
    r"\d+\s*%\s*(?:de\s+)?menos\s+faltas",
    r"reduz(?:em|iu|ir)?\s+(?:\w+\s+){0,3}faltas",
    r"mais\s+de\s+\d+\s+psic[oó]logas",
    r"nunca\s+perceberam",
    r"\b(?:Marina|Camila|Let[ií]cia|Mariana)\s+[A-Z]\.",
]


def texto_visivel(fonte: str) -> str:
    fonte = re.sub(r"<!--.*?-->", " ", fonte, flags=re.S)
    fonte = re.sub(r"<(script|style)\b.*?</\1>", " ", fonte, flags=re.S | re.I)
    fonte = re.sub(r"<[^>]+>", " ", fonte)
    return re.sub(r"\s+", " ", html.unescape(fonte))


def main() -> int:
    base = sys.argv[1].rstrip("/")
    achados = 0
    for p in PAGINAS:
        with urllib.request.urlopen(base + p, timeout=20) as r:
            status, corpo = r.status, r.read().decode("utf-8", "replace")
        texto = texto_visivel(corpo)
        hits = [m.group(0) for pad in PADROES for m in re.finditer(pad, texto, re.I)]
        print(f"{p:18} {status}  {len(texto):6} chars  " + ("LIMPO" if not hits else f"ACHOU {hits}"))
        achados += len(hits)
    print("RESULTADO:", "LIMPO" if achados == 0 else f"{achados} ocorrência(s)")
    return 1 if achados else 0


if __name__ == "__main__":
    sys.exit(main())
