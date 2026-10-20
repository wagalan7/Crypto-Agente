"""Backup pré-release R6 — roda DENTRO do contêiner de produção (railway ssh).

Mesmas regras do backup pré-R1 (RELEASE-R1-LANDING.md §4):
- banco ativo aberto só com mode=ro; cópia por sqlite3 backup();
- tudo em TemporaryDirectory no disco efêmero; nada escrito em /data;
- nenhum app/worker iniciado; só config + backup_service (cliente S3 do produto);
- bucket privado já usado pelos backups diários; prefixo sob consultorio/ e
  contracts/, que herdam a retenção; nada apagado (_cleanup_old_backups não é
  chamado) e .backup_state intocado;
- objetos baixados de volta e conferidos (sha256, integrity_check, contagens);
- saída só com chaves, tamanhos, sha256 e contagens — nenhum conteúdo, nenhum segredo.
"""
import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, "/app")
os.chdir("/app")
import config                 # noqa: E402
import backup_service as bs   # noqa: E402

assert bs._is_configured(), "backup S3 não configurado"
s3 = bs._get_s3_client()
BUCKET = config.BACKUP_S3_BUCKET
STAMP = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
CHAVE_DB = f"consultorio/pre-release-R6/consultorio-{STAMP}.db.gz"
CHAVE_CT = f"contracts/pre-release-R6/contracts-{STAMP}.tar.gz"


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def contagens(path):
    c = sqlite3.connect(path)
    t = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    chave = {n: c.execute(f"SELECT COUNT(*) FROM '{n}'").fetchone()[0]
             for n in ("tenants", "appointments", "patients", "contracts", "conversations") if n in t}
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    return len(t), chave, ok


with tempfile.TemporaryDirectory(prefix="backup-r6-") as tmp:
    # ── banco ──
    copia = os.path.join(tmp, "consultorio.db")
    ativo = sqlite3.connect("file:/data/consultorio.db?mode=ro", uri=True)
    dst = sqlite3.connect(copia)
    ativo.backup(dst)
    dst.close()
    ativo.close()
    n, chave, ok = contagens(copia)
    gz = copia + ".gz"
    with open(copia, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    s_gz = sha(gz)
    s3.upload_file(gz, BUCKET, CHAVE_DB)
    print(f"banco     {os.path.getsize(copia)/1e6:.2f} MB -> {os.path.getsize(gz)/1e6:.2f} MB gz | sha256 {s_gz}")
    print(f"          -> {CHAVE_DB} | tabelas {n} | integrity {ok} | {chave}")

    # ── contratos ──
    pasta = "/data/contracts"
    arquivos = []
    for raiz, _, nomes in os.walk(pasta):
        arquivos += [os.path.join(raiz, x) for x in nomes]
    tgz = os.path.join(tmp, "contracts.tar.gz")
    with tarfile.open(tgz, "w:gz") as tar:
        for a in arquivos:
            tar.add(a, arcname=os.path.relpath(a, pasta))
    s_tgz = sha(tgz)
    s3.upload_file(tgz, BUCKET, CHAVE_CT)
    total = sum(os.path.getsize(a) for a in arquivos)
    print(f"contratos {len(arquivos)} arquivos, {total/1e6:.2f} MB -> {os.path.getsize(tgz)/1e6:.2f} MB tgz | sha256 {s_tgz}")
    print(f"          -> {CHAVE_CT}")

    # ── conferência: baixar de volta ──
    volta_db = os.path.join(tmp, "volta.db.gz")
    volta_ct = os.path.join(tmp, "volta.tar.gz")
    s3.download_file(BUCKET, CHAVE_DB, volta_db)
    s3.download_file(BUCKET, CHAVE_CT, volta_ct)
    rest = os.path.join(tmp, "restaurado.db")
    with gzip.open(volta_db, "rb") as fi, open(rest, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    n2, chave2, ok2 = contagens(rest)
    with tarfile.open(volta_ct, "r:gz") as tar:
        membros = len(tar.getmembers())
    print("conferencia: sha banco", "IGUAL" if sha(volta_db) == s_gz else "DIFERENTE",
          "| sha contratos", "IGUAL" if sha(volta_ct) == s_tgz else "DIFERENTE",
          f"| restaurado: tabelas {n2}, integrity {ok2}, contagens {'IGUAIS' if chave2 == chave else chave2}",
          f"| tar com {membros} membros")
print("temporario apagado:", not os.path.exists(tmp))
print("FIM")
