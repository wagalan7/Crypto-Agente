"""CLI offline do R10A. Lê JSON explícito, imprime JSON; não carrega .env.

Uso: backend/.venv311/bin/python -B backend/scripts/research_replay.py arquivo.json
Nenhuma consulta remota, credencial, ordem ou escrita no banco.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Replay OHLCV offline — pesquisa, sem promoção LIVE")
    parser.add_argument("input", nargs="?", type=Path, help="dataset JSON local explícito")
    parser.add_argument("--manifest", action="store_true", help="mostra contrato e limitações")
    args = parser.parse_args(argv)
    from services.offline_replay_service import replay_manifest, run_payload
    try:
        if args.manifest:
            result = replay_manifest()
        elif args.input is not None:
            # Limite também após read: não confia apenas em stat (TOCTOU).
            with args.input.open("rb") as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("dataset maior que 16 MiB")
            def reject_constant(value):
                raise ValueError("constante JSON não finita")
            payload = json.loads(raw, parse_constant=reject_constant)
            result = run_payload(payload)
        else:
            parser.error("informe um dataset ou --manifest")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError):
        print(json.dumps({"status": "INVALID_INPUT", "promotable": False,
                          "detail": "Confira o schema do dataset e os limites do manifesto."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
