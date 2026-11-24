"""
Cache TTL Thread-Safe e Controle de Cooldown (FyNex Quant AI)
=============================================================
Substitui caches ad-hoc com `dict + Lock` espalhados pelo sistema por uma
abstração única, limitada em memória (LRU) e segura para concorrência.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable, Generic, Hashable, Optional, Tuple, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Cache em memória thread-safe com expiração por TTL e remoção LRU quando cheio."""

    def __init__(self, max_size: int = 128, default_ttl: float = 5.0):
        self.max_size = max(1, int(max_size))
        self.default_ttl = float(default_ttl)
        self._lock = threading.Lock()
        self._store: "OrderedDict[K, Tuple[float, V]]" = OrderedDict()

    def get(self, key: K, ttl_seconds: Optional[float] = None) -> Tuple[bool, Optional[V]]:
        """Retorna `(True, valor)` em cache hit válido ou `(False, None)` se ausente/expirado."""
        ttl = self.default_ttl if ttl_seconds is None else float(ttl_seconds)
        if ttl <= 0:
            return False, None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False, None
            ts, val = entry
            if (now - ts) >= ttl:
                self._store.pop(key, None)
                return False, None
            self._store.move_to_end(key)
            return True, val

    def peek(self, key: K) -> Tuple[bool, Optional[V]]:
        """Retorna o valor armazenado independentemente do TTL (útil para fallback offline)."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False, None
            return True, entry[1]

    def set(self, key: K, value: V) -> V:
        """Armazena `value` sob `key` atualizando timestamp e aplicando limite máximo."""
        now = time.monotonic()
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            elif len(self._store) >= self.max_size:
                self._store.popitem(last=False)
            self._store[key] = (now, value)
        return value

    def get_or_compute(self, key: K, factory: Callable[[], V], ttl_seconds: Optional[float] = None) -> V:
        """Retorna do cache se válido; caso contrário executa `factory()` e armazena o resultado."""
        hit, val = self.get(key, ttl_seconds=ttl_seconds)
        if hit:
            return val  # type: ignore[return-value]
        computed = factory()
        return self.set(key, computed)

    def invalidate(self, key: K) -> None:
        with self._lock:
            self._store.pop(key, None)

    def delete_matching(self, predicate: Callable[[K], bool]) -> int:
        """Remove todas as entradas cuja chave satisfaz `predicate`."""
        with self._lock:
            keys = [k for k in self._store if predicate(k)]
            for k in keys:
                self._store.pop(k, None)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


class CooldownTracker:
    """Controla intervalos mínimos entre ações nomeadas (ex.: refresh forçado ou deduplicação)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ts: dict[Hashable, float] = {}

    def allow(self, key: Hashable, cooldown_sec: float) -> bool:
        """Retorna True se o cooldown expirou e atualiza o carimbo de tempo."""
        now = time.monotonic()
        with self._lock:
            last = self._last_ts.get(key)
            if last is not None and (now - last) < cooldown_sec:
                return False
            self._last_ts[key] = now
            return True

    def clear(self) -> None:
        with self._lock:
            self._last_ts.clear()
