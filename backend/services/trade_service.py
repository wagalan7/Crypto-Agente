"""
Armazenamento em memória de trades por usuário (sincronização entre dispositivos).
Dados são perdidos ao reiniciar o servidor — localStorage é a fonte primária.
"""
from __future__ import annotations
from typing import Dict, List, Any

# user_id → list of trade dicts
_store: Dict[str, List[Dict[str, Any]]] = {}


def get_trades(user_id: str) -> List[Dict[str, Any]]:
    return _store.get(user_id, [])


def save_trades(user_id: str, trades: List[Dict[str, Any]]) -> None:
    _store[user_id] = trades


def delete_trades(user_id: str) -> None:
    _store.pop(user_id, None)
