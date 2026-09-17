"""CLI do R10B: exporta dataset local das vetadas R09 para o comparador R10A.

Validação (sem banco):
  backend/.venv311/bin/python -B backend/scripts/research_dataset.py --request req.json --validate-only
Exportação (leitura explícita; DATABASE_URL vem do ambiente, nunca de argumento):
  backend/.venv311/bin/python -B backend/scripts/research_dataset.py --request req.json --read-db --out-dir /fora/do/repo/export-1
Análise, comando separado:
  backend/.venv311/bin/python -B backend/scripts/research_replay.py /fora/do/repo/export-1/dataset.json

Não carrega `.env`, não chama `init_db`, não escreve no banco, não sobrescreve
arquivos e nunca imprime DSN, credenciais ou texto de exceção.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_REQUEST_BYTES = 1024 * 1024


class _SourceUnavailable(Exception):
    pass


def _fail(status: str, code: int) -> int:
    print(json.dumps({"status": status, "promotable": False,
                      "detail": "Confira a requisição, o destino e a documentação R10B."}),
          file=sys.stderr)
    return code


def _read_request(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("requisição grande demais")

    def reject(constant):
        raise ValueError("constante JSON não finita")
    return json.loads(raw, parse_constant=reject)


async def _export(request, out_dir):
    from services.research_dataset_service import load_dataset, write_artifacts
    import db  # somente neste ramo explícito; db.py não lê `.env`
    if not db.DB_ENABLED:
        raise _SourceUnavailable()
    try:
        async with db.get_session() as session:
            dataset, manifest = await load_dataset(session, request)
    except Exception as exc:
        from services.research_dataset_service import DatasetError
        if isinstance(exc, DatasetError):
            raise
        raise _SourceUnavailable() from None
    finally:
        if db._engine is not None:
            await db._engine.dispose()
    target = write_artifacts(out_dir, dataset, manifest)
    return target, manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Exportação local R10B (somente leitura)")
    parser.add_argument("--request", type=Path, required=True, help="requisição JSON explícita")
    parser.add_argument("--validate-only", action="store_true", help="valida sem acessar banco")
    parser.add_argument("--read-db", action="store_true", help="autoriza a leitura do banco")
    parser.add_argument("--out-dir", type=Path, help="diretório NOVO fora do repositório")
    args = parser.parse_args(argv)
    if args.validate_only == args.read_db:
        return _fail("INVALID_USAGE", 2)
    if args.read_db != (args.out_dir is not None):
        return _fail("INVALID_USAGE", 2)
    from services.research_dataset_service import (
        DatasetError, DatasetLimitError, check_output_dir, parse_request,
    )
    try:
        request = parse_request(_read_request(args.request))
        if args.read_db:
            check_output_dir(args.out_dir)
    except (DatasetError, ValueError, OSError):
        return _fail("INVALID_REQUEST", 2)
    if args.validate_only:
        print(json.dumps({"status": "VALID_REQUEST", "request_hash": request.request_hash(),
                          "management_changed_parameters": list(request.changed),
                          "horizon_bars": request.horizon_bars, "promotable": False}))
        return 0
    try:
        target, manifest = asyncio.run(_export(request, args.out_dir))
    except DatasetLimitError:
        return _fail("DATASET_LIMIT", 3)
    except DatasetError:
        return _fail("SOURCE_CONTRACT_OR_OUTPUT_ERROR", 3)
    except (_SourceUnavailable, OSError):
        return _fail("SOURCE_UNAVAILABLE", 3)
    print(json.dumps({"status": manifest["state"], "out_dir": str(target),
                      "request_hash": manifest["request_hash"],
                      "dataset_sha256": manifest["fingerprints"]["dataset_sha256"],
                      "counts": manifest["counts"], "promotable": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
