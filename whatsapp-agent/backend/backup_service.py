"""Backup automático do SQLite para storage S3-compatível (Cloudflare R2, B2, AWS S3).

Uso: chamar `run_backup_if_due()` periodicamente (scheduler já roda a cada 30 min).
Faz backup uma vez por dia, mantém retenção configurável e remove backups antigos.

Configuração via env vars (todos opcionais — se ausentes, backup é no-op):
- BACKUP_S3_BUCKET
- BACKUP_S3_ENDPOINT_URL  (https://<account>.r2.cloudflarestorage.com)
- BACKUP_S3_ACCESS_KEY_ID
- BACKUP_S3_SECRET_ACCESS_KEY
- BACKUP_S3_REGION (default: auto)
- BACKUP_RETENTION_DAYS (default: 30)
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from database import DB_PATH

logger = logging.getLogger(__name__)

_STATE_FILE = Path(config.DATA_DIR) / ".backup_state"


def _is_configured() -> bool:
    return bool(
        config.BACKUP_S3_BUCKET
        and config.BACKUP_S3_ENDPOINT_URL
        and config.BACKUP_S3_ACCESS_KEY_ID
        and config.BACKUP_S3_SECRET_ACCESS_KEY
    )


def _get_s3_client():
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=config.BACKUP_S3_ENDPOINT_URL,
        aws_access_key_id=config.BACKUP_S3_ACCESS_KEY_ID,
        aws_secret_access_key=config.BACKUP_S3_SECRET_ACCESS_KEY,
        region_name=config.BACKUP_S3_REGION,
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _last_backup_date() -> str | None:
    """Retorna 'YYYY-MM-DD' do último backup feito, ou None."""
    try:
        return _STATE_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _mark_backup_done(date_str: str) -> None:
    try:
        _STATE_FILE.write_text(date_str)
    except Exception as e:
        logger.warning(f"[backup] Falha ao gravar estado: {e}")


def _dump_db_to_file(dest_path: str) -> None:
    """Faz dump consistente do SQLite (com lock) para um arquivo .db local."""
    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _gzip_file(src_path: str, dest_path: str) -> int:
    """Comprime src_path → dest_path.gz, retorna tamanho final em bytes."""
    with open(src_path, "rb") as f_in, gzip.open(dest_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    return os.path.getsize(dest_path)


def _cleanup_old_backups(s3, prefix: str = "consultorio/") -> int:
    """Remove backups mais antigos que BACKUP_RETENTION_DAYS dias. Retorna nº removidos."""
    if config.BACKUP_RETENTION_DAYS <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.BACKUP_RETENTION_DAYS)
    removed = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        to_delete = []
        for page in paginator.paginate(Bucket=config.BACKUP_S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                if obj["LastModified"] < cutoff:
                    to_delete.append({"Key": obj["Key"]})
        # delete em lotes de 1000
        for i in range(0, len(to_delete), 1000):
            batch = to_delete[i : i + 1000]
            if batch:
                s3.delete_objects(Bucket=config.BACKUP_S3_BUCKET, Delete={"Objects": batch})
                removed += len(batch)
    except Exception as e:
        logger.warning(f"[backup] Falha na limpeza: {e}")
    return removed


def run_backup_if_due() -> dict:
    """Executa backup se ainda não foi feito hoje. Retorna dict com status.
    Seguro para chamar repetidamente — só sobe uma vez por dia.
    """
    if not _is_configured():
        return {"status": "skipped", "reason": "not_configured"}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_backup_date() == today:
        return {"status": "skipped", "reason": "already_done_today"}

    if not os.path.exists(DB_PATH):
        return {"status": "error", "reason": "db_not_found"}

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_path = os.path.join(tmpdir, "consultorio.db")
            gz_path = os.path.join(tmpdir, "consultorio.db.gz")
            _dump_db_to_file(raw_path)
            size_gz = _gzip_file(raw_path, gz_path)

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            key = f"consultorio/{today}/consultorio-{timestamp}.db.gz"

            s3 = _get_s3_client()
            s3.upload_file(gz_path, config.BACKUP_S3_BUCKET, key)
            removed = _cleanup_old_backups(s3)

        _mark_backup_done(today)
        logger.info(f"[backup] OK → s3://{config.BACKUP_S3_BUCKET}/{key} ({size_gz} bytes, antigos removidos: {removed})")
        return {"status": "ok", "key": key, "size_gz": size_gz, "removed_old": removed}
    except Exception as e:
        logger.exception(f"[backup] Falha: {e}")
        return {"status": "error", "reason": str(e)}
