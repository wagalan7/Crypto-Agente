#!/usr/bin/env python3
"""
Relatório de MARCO do sweep do universo — estilo "lote 1".

A cada múltiplo de `--step` moedas verificadas (done), monta o bloco:

    📍 Marco: sweep em done=75/433, errors=0, agora na ACH. 4 candidatas agora:
    Moeda        wf_avg_r  avg_r   n    WR      expiry
    HOT          0.996     1.088   102  97.0%   1.0%
    ...
    WOO entrou. Faltam ~25 moedas; próximo ping na conclusão (done=100).

Fonte: /api/backtest/universe/status (progresso) + /api/backtest/universe/ranking
(candidatas a promover = `candidates_to_promote`, já filtradas pelo min_calib do
servidor). Marca "(nova)" comparando as bases com o snapshot do último marco.

Mantém estado entre execuções num arquivo JSON (--state) para (a) só falar quando
um novo marco de `--step` é cruzado e (b) detectar quem ENTROU na lista. Assim a
função pode ser chamada a cada ciclo de polling — fica QUIETA (exit 0, sem stdout)
quando nada de novo, e imprime o bloco só quando há marco a reportar.

Uso:
    python sweep_milestone.py                      # quieto, a não ser que cruze marco
    python sweep_milestone.py --force              # imprime o estado atual sempre
    python sweep_milestone.py --step 25 --base URL --state /tmp/sweep_state.json
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import urllib.request

DEFAULT_BASE = "https://crypto-agente-production.up.railway.app"
DEFAULT_STATE = "/tmp/sweep_milestone_state.json"


def _get(base: str, path: str) -> dict:
    url = f"{base}{path}{'&' if '?' in path else '?'}t={int(time.time())}"
    req = urllib.request.Request(url, headers={"User-Agent": "sweep-milestone/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _split_current(current: str) -> tuple[str, str]:
    """'AUCTION/USDT:USDT 4h' -> ('AUCTION', '4h')."""
    if not isinstance(current, str) or not current.strip():
        return "?", ""
    parts = current.split()
    tf = parts[1] if len(parts) > 1 else ""
    base = parts[0].split("/")[0]
    return base, tf


def status_line(base_url: str) -> str:
    """Resposta de status estilo lote 1 (1-2 frases), p/ quando o usuário pergunta."""
    status = _get(base_url, "/api/backtest/universe/status")
    p = status.get("progress", status)
    done = int(p.get("done") or 0)
    total = int(p.get("total") or 0)
    computed = int(p.get("computed") or 0)
    errors = int(p.get("errors") or 0)
    running = bool(p.get("running"))
    finished = bool(p.get("finished_at")) or (not running and total and done >= total)
    cur_base, tf = _split_current(p.get("current") or "")
    if not tf:
        tfs = p.get("tfs") or []
        tf = tfs[0] if tfs else "4h"

    if finished:
        return (f"Sweep CONCLUÍDO: done:{done}/{total}, computed:{computed}, "
                f"errors:{errors}. Trago a lista final de candidatas pra você revisar a promoção.")

    remaining = max(total - done, 0)
    falta = f"Reta final — faltam {remaining}." if remaining <= 10 else f"Faltam {remaining}."
    return (f"Sweep: done:{done}/{total}, computed:{computed}, errors:{errors}, "
            f"agora na {cur_base} ({tf}). {falta} O monitor te avisa na conclusão "
            f"(done={total}) e aí trago a lista final de candidatas pra você revisar a promoção.")


def _fmt_table(rows: list[dict], new_bases: set[str]) -> str:
    header = ["Moeda", "wf_avg_r", "avg_r", "n", "WR", "expiry"]
    lines = [header]
    for c in rows:
        base = c.get("base") or c.get("symbol", "")
        label = f"{base} (nova)" if base in new_bases else base
        lines.append([
            label,
            f"{c.get('wf_avg_r', 0):.3f}",
            f"{c.get('avg_r', 0):.3f}",
            str(c.get("n_trades", c.get("n", ""))),
            f"{c.get('wr_clean_pct', c.get('wr_pct', 0)):.1f}%",
            f"{c.get('expiry_pct', 0):.1f}%",
        ])
    widths = [max(len(r[i]) for r in lines) for i in range(len(header))]
    out = []
    for r in lines:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)))
    return "\n".join(out)


def build_report(base_url: str, step: int, state_path: str, force: bool) -> tuple[str | None, dict]:
    status = _get(base_url, "/api/backtest/universe/status")
    p = status.get("progress", status)
    done = int(p.get("done") or 0)
    total = int(p.get("total") or 0)
    errors = int(p.get("errors") or 0)
    current = p.get("current") or "?"
    running = bool(p.get("running"))
    finished_at = p.get("finished_at")

    state = _load_state(state_path)
    last_marker = int(state.get("last_marker", -1))
    prev_bases = set(state.get("candidate_bases", []))

    marker = (done // step) * step
    finished = bool(finished_at) or (not running and total and done >= total)
    crossed = marker > 0 and marker != last_marker
    should_report = force or crossed or (finished and not state.get("reported_finish"))
    if not should_report:
        return None, state

    rank = _get(base_url, "/api/backtest/universe/ranking")
    cands = rank.get("candidates_to_promote") or []
    cands = sorted(cands, key=lambda c: c.get("wf_avg_r", 0), reverse=True)
    cur_bases = [c.get("base") or c.get("symbol", "") for c in cands]
    new_bases = set(cur_bases) - prev_bases

    cur_short = current.split("/")[0] if isinstance(current, str) else current
    if finished:
        head = f"✅ Sweep concluído: done={done}/{total}, errors={errors}. {len(cands)} candidatas:"
    else:
        head = (f"📍 Marco: sweep em done={done}/{total}, errors={errors}, "
                f"agora na {cur_short}. {len(cands)} candidatas agora:")

    parts = [head, _fmt_table(cands, new_bases)]
    if new_bases:
        joined = ", ".join(sorted(new_bases))
        verb = "entrou" if len(new_bases) == 1 else "entraram"
        parts.append(f"{joined} {verb}.")
    if finished:
        parts.append("Concluído — sem próximo ping.")
    else:
        remaining = max(total - done, 0)
        next_marker = marker + step
        nxt = "na conclusão" if next_marker >= total else f"em done={next_marker}"
        parts.append(f"Faltam ~{remaining} moedas; próximo ping {nxt}.")

    new_state = {
        "last_marker": marker if not finished else last_marker,
        "candidate_bases": cur_bases,
        "reported_finish": finished or state.get("reported_finish", False),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return "\n".join(parts), new_state


def main() -> int:
    ap = argparse.ArgumentParser(description="Relatório de marco do sweep (estilo lote 1).")
    ap.add_argument("--base", default=os.environ.get("SWEEP_BASE", DEFAULT_BASE))
    ap.add_argument("--step", type=int, default=25, help="Tamanho do marco (default 25).")
    ap.add_argument("--state", default=os.environ.get("SWEEP_STATE", DEFAULT_STATE))
    ap.add_argument("--force", action="store_true", help="Imprime o estado atual mesmo sem novo marco.")
    ap.add_argument("--status", action="store_true", help="Imprime a resposta de status (estilo lote 1) e sai.")
    args = ap.parse_args()

    if args.status:
        print(status_line(args.base))
        return 0

    report, new_state = build_report(args.base, args.step, args.state, args.force)
    if report is None:
        return 0
    print(report)
    _save_state(args.state, new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
