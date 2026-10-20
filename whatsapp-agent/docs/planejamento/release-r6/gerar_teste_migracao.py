"""Gera o script que roda DENTRO do contêiner de produção para testar a
migração da versão candidata (R2 + main + E08B) sobre uma CÓPIA do banco real.

Regras do script gerado:
- banco ativo aberto só com mode=ro; a cópia sai por sqlite3 backup();
- tudo em TemporaryDirectory no disco efêmero, apagado no fim; nada em /data;
- nenhum app ou worker iniciado: só init_db() da versão candidata, na cópia;
- saída só com nomes de tabela/coluna, contagens, tempos e integrity_check —
  nenhum conteúdo de linha, nenhum segredo;
- nada sai do contêiner além desse texto.

Uso: python gerar_teste_migracao.py <git-dir> <commit> <saida.py>
"""
import base64
import subprocess
import sys

GIT, COMMIT, SAIDA = sys.argv[1:4]
MODULOS = ["database.py", "config.py", "crypto.py", "noticias.py", "noticias_job.py"]

embutidos = {}
for m in MODULOS:
    conteudo = subprocess.check_output(["git", "-C", GIT, "show", f"{COMMIT}:whatsapp-agent/backend/{m}"])
    embutidos[m] = base64.b64encode(conteudo).decode()

script = '''
import base64, os, sqlite3, sys, tempfile, time, hashlib
MODULOS = %r
COMMIT = %r

def esquema(conn):
    t = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%%' ORDER BY name")]
    cols = {n: [c[1] for c in conn.execute(f"PRAGMA table_info('{n}')")] for n in t}
    cont = {n: conn.execute(f"SELECT COUNT(*) FROM '{n}'").fetchone()[0] for n in t}
    idx = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%%'"))
    return t, cols, cont, idx

print("versao candidata:", COMMIT)
with tempfile.TemporaryDirectory(prefix="teste-migracao-") as tmp:
    codigo, dados = os.path.join(tmp, "codigo"), os.path.join(tmp, "dados")
    os.makedirs(codigo); os.makedirs(dados)
    for nome, b64 in MODULOS.items():
        with open(os.path.join(codigo, nome), "wb") as f:
            f.write(base64.b64decode(b64))
    ativo = sqlite3.connect("file:/data/consultorio.db?mode=ro", uri=True)
    copia_path = os.path.join(dados, "consultorio.db")
    copia = sqlite3.connect(copia_path)
    t0 = time.time(); ativo.backup(copia); copia.close()
    print(f"copia: {os.path.getsize(copia_path)/1e6:.2f} MB em {time.time()-t0:.1f}s (ativo aberto so leitura)")
    c = sqlite3.connect(copia_path)
    t_antes, cols_antes, cont_antes, idx_antes = esquema(c)
    print("antes: tabelas", len(t_antes), "| integrity_check", c.execute("PRAGMA integrity_check").fetchone()[0])
    c.close()

    # init_db da versao candidata, NA COPIA (DATA_DIR aponta para o temporario)
    os.environ["DATA_DIR"] = dados
    sys.path = [codigo] + [p for p in sys.path if p not in ("", ".", "/app")]
    os.chdir(tmp)
    import database as db
    assert os.path.realpath(db.DB_PATH) == os.path.realpath(copia_path), db.DB_PATH
    assert os.path.dirname(db.__file__) == codigo, db.__file__
    t0 = time.time(); db.init_db(); t1 = time.time() - t0
    t0 = time.time(); db.init_db(); t2 = time.time() - t0
    print(f"init_db: 1a execucao {t1:.2f}s | 2a execucao (idempotencia) {t2:.2f}s")

    c = sqlite3.connect(copia_path)
    t_dep, cols_dep, cont_dep, idx_dep = esquema(c)
    print("depois: tabelas", len(t_dep), "| integrity_check", c.execute("PRAGMA integrity_check").fetchone()[0],
          "| foreign_key_check", len(c.execute("PRAGMA foreign_key_check").fetchall()), "violacoes")
    print("tabelas novas:", sorted(set(t_dep) - set(t_antes)))
    print("tabelas que sumiram:", sorted(set(t_antes) - set(t_dep)))
    novas_cols = {n: sorted(set(cols_dep[n]) - set(cols_antes[n])) for n in t_antes if n in cols_dep and set(cols_dep[n]) - set(cols_antes[n])}
    print("colunas novas em tabelas existentes:", novas_cols)
    sumidas = {n: sorted(set(cols_antes[n]) - set(cols_dep.get(n, []))) for n in t_antes if set(cols_antes[n]) - set(cols_dep.get(n, []))}
    print("colunas que sumiram:", sumidas)
    difs = {n: (cont_antes[n], cont_dep.get(n)) for n in t_antes if cont_antes[n] != cont_dep.get(n)}
    print("contagens diferentes em tabelas existentes:", difs)
    print("linhas nas tabelas novas:", {n: cont_dep[n] for n in sorted(set(t_dep) - set(t_antes))})
    print("indices novos:", sorted(set(idx_dep) - set(idx_antes)))
    print("contagens-chave:", {n: cont_dep[n] for n in ("tenants", "appointments", "patients", "contracts", "slot_offers") if n in cont_dep})
    c.close()

a = sqlite3.connect("file:/data/consultorio.db?mode=ro", uri=True)
print("banco ATIVO depois do teste: tabelas", a.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%%'").fetchone()[0],
      "| integrity_check", a.execute("PRAGMA integrity_check").fetchone()[0])
print("temporario apagado:", not os.path.exists(tmp))
print("FIM")
''' % (embutidos, COMMIT)

with open(SAIDA, "w") as f:
    f.write(script)
print("gerado", SAIDA, len(script) // 1024, "KB")
