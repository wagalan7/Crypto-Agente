"""Compara o TEXTO VISÍVEL antes (8771) x depois (8772), página a página.

Regra: a única inserção permitida é o texto do celular ilustrativo do herói, e
cada frase dele tem de existir na página antiga (falas do exemplo de conversa).
A única remoção permitida é o artefato '"/>' do favicon SVG antigo (o '>' dentro
do data-URI quebra o removedor de tags; não é texto que o visitante vê).
Título, meta description, og:title e og:description não podem mudar.

Uso: python3 texto_igual.py <dir-com-alegacoes_texto.py>
"""
import difflib
import re
import sys
import urllib.request

sys.path.insert(0, sys.argv[1])
from alegacoes_texto import texto_visivel  # noqa: E402

SEGS = ["nutricao", "fisioterapia", "fonoaudiologia", "odontologia", "terapia", "estetica",
        "advocacia", "contabilidade", "salao", "barbearia", "petshop", "oficina"]
ARTEFATO_FAVICON = ' "/>'


def get(porta, c):
    return urllib.request.urlopen(f"http://127.0.0.1:{porta}{c}", timeout=20).read().decode()


def metas(h):
    return (re.findall(r"<title>(.*?)</title>", h, re.S),
            re.findall(r'<meta (?:name|property)="(?:description|og:title|og:description)" content="(.*?)"', h))


falhas = 0
for c in ["/"] + [f"/p/{s}" for s in SEGS]:
    ha, hd = get(8771, c), get(8772, c)
    a, d = texto_visivel(ha), texto_visivel(hd)
    ops = [o for o in difflib.SequenceMatcher(None, a, d, autojunk=False).get_opcodes() if o[0] != "equal"]
    removido = [a[i1:i2] for op, i1, i2, j1, j2 in ops if op in ("delete", "replace")]
    inserido = [d[j1:j2] for op, i1, i2, j1, j2 in ops if op in ("insert", "replace")]
    frases = [f.strip() for bloco in inserido for f in re.split(r"\s\d\d:\d\d\s?|\s(?=Quarta|Quinta|Confira)", bloco)
              if len(f.strip()) > 3]
    novas = [f for f in frases if f not in a]
    ok = (metas(ha) == metas(hd) and all(r == ARTEFATO_FAVICON for r in removido)
          and len(inserido) == 1 and frases and not novas)
    falhas += not ok
    print(f"{c:20} meta={'igual' if metas(ha) == metas(hd) else 'DIFERENTE'}  removido={removido}  "
          f"inserções={len(inserido)} frases={len(frases)} novas={novas}  {'OK' if ok else 'FALHA'}")
print("RESULTADO:", "OK — só o texto do celular, todo já presente na página" if not falhas else f"{falhas} falha(s)")
sys.exit(1 if falhas else 0)
