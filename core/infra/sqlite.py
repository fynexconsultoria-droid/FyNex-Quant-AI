"""
Camada Unificada de Conexão e Migração SQLite (FyNex Quant AI)
==============================================================
Centraliza abertura de conexões SQLite com PRAGMAs consistentes (WAL,
busy_timeout, synchronous=NORMAL, cache em memória), modo somente leitura
para consultas de dashboard/chat e registro de schemas inicializados.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, Optional, Tuple

_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ensure_wal_mode(db_path: str, timeout: float = 15.0) -> None:
    """Ativa journal_mode=WAL fora de transação apenas se o banco ainda não estiver em WAL."""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)};")
        cur_mode = str((conn.execute("PRAGMA journal_mode;").fetchone() or [""])[0]).lower()
        if cur_mode != "wal":
            conn.execute("PRAGMA journal_mode=WAL;")
    finally:
        conn.close()


def connect_sqlite(
    db_path: str,
    *,
    timeout: float = 15.0,
    readonly: bool = False,
    configure_pragmas: bool = True,
    isolation_level: Optional[str] = "",
) -> sqlite3.Connection:
    """Abre uma conexão SQLite configurada com `row_factory=sqlite3.Row` e busy_timeout."""
    if not readonly and db_path and db_path != ":memory:":
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout, isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)};")
    if readonly:
        conn.execute("PRAGMA query_only=ON;")
    elif configure_pragmas:
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA cache_size=-4000;")
        conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


@contextmanager
def sqlite_connection(
    db_path: str,
    *,
    timeout: float = 15.0,
    readonly: bool = False,
    configure_pragmas: bool = True,
    write_immediate: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Context manager que garante transação (incluindo BEGIN IMMEDIATE) e fechamento seguro."""
    conn = connect_sqlite(
        db_path,
        timeout=timeout,
        readonly=readonly,
        configure_pragmas=configure_pragmas,
        isolation_level=None if write_immediate else "",
    )
    try:
        if readonly:
            yield conn
        elif write_immediate:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                if conn.in_transaction:
                    conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        else:
            with conn:
                yield conn
    finally:
        conn.close()


@contextmanager
def sqlite_readonly_optional(
    db_path: str,
    *,
    timeout: float = 15.0,
) -> Iterator[Optional[sqlite3.Connection]]:
    """Abre conexão somente leitura ou produz `None` caso o arquivo ainda não exista."""
    if not db_path or not os.path.exists(db_path):
        yield None
        return
    with sqlite_connection(db_path, timeout=timeout, readonly=True) as conn:
        yield conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Verifica de forma segura se `table_name` existe no banco SQLite."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def ensure_column(cursor: sqlite3.Cursor, table: str, column: str, col_def: str) -> None:
    """Executa migração idempotente adicionando `column` em `table` apenas se ainda não existir."""
    if not _SQL_IDENT_RE.match(table) or not _SQL_IDENT_RE.match(column):
        raise ValueError(f"Identificador SQL inválido: {table}.{column}")
    cols = {row[1] for row in cursor.execute(f"PRAGMA table_info({table});").fetchall()}
    if column not in cols:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def};")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


class SchemaRegistry:
    """
    Evita reexecutar DDLs a cada instanciação de Store, mas re-inicializa
    automaticamente se o arquivo SQLite for recriado/truncado (útil em testes).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized: Dict[Tuple[str, str], Tuple[int, int]] = {}

    @staticmethod
    def _file_signature(abs_path: str) -> Tuple[int, int]:
        if not os.path.exists(abs_path):
            return (-1, -1)
        st = os.stat(abs_path)
        return (st.st_ino, 1 if st.st_size > 0 else 0)

    def ensure_initialized(
        self,
        schema_name: str,
        db_path: str,
        init_fn: Callable[[], None],
    ) -> None:
        abs_path = os.path.abspath(db_path)
        key = (schema_name, abs_path)
        with self._lock:
            sig = self._file_signature(abs_path)
            if self._initialized.get(key) != sig or sig[0] == -1 or sig[1] == 0:
                init_fn()
                self._initialized[key] = self._file_signature(abs_path)

    def reset(self, schema_name: Optional[str] = None) -> None:
        with self._lock:
            if schema_name is None:
                self._initialized.clear()
            else:
                for k in [k for k in self._initialized if k[0] == schema_name]:
                    self._initialized.pop(k, None)


SCHEMA_REGISTRY = SchemaRegistry()
