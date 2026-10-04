"""Static checks on LLM-generated SQL before it reaches Postgres.

This is defense in depth, not the only control: the query also runs as a read-only, RLS-bound role
with a statement timeout. The guard exists to give the model fast, specific feedback and to block
whole classes of abuse (writes, catalog snooping, sleep/DoS functions) up front.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {"sales", "stores", "products", "regions", "customers_v"}
ALLOWED_SCHEMAS = {None, "", "analytics"}
MAX_ROWS = 500

FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.TruncateTable, exp.Copy, exp.Command, exp.Into, exp.Lock, exp.Grant, exp.Set,
)
FORBIDDEN_FUNC_PREFIXES = ("pg_", "lo_", "dblink", "set_config", "current_setting", "query_to_xml",
                           "txid_", "inet_", "has_", "file_")


class SqlRejected(ValueError):
    pass


@dataclass
class GuardResult:
    sql: str
    tables: set[str]
    limit_applied: bool


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def check_sql(sql: str, max_rows: int = MAX_ROWS) -> GuardResult:
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise SqlRejected(f"could not parse SQL: {str(e).splitlines()[0]}") from e
    if len(statements) != 1:
        raise SqlRejected("exactly one statement is allowed")
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        raise SqlRejected(f"only SELECT queries are allowed, got {stmt.key.upper()}")

    for node in stmt.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlRejected(f"{node.key.upper()} is not allowed")
        if isinstance(node, exp.Func):
            name = _func_name(node)
            if name.startswith(FORBIDDEN_FUNC_PREFIXES):
                raise SqlRejected(f"function {name}() is not allowed")

    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    tables: set[str] = set()
    for t in stmt.find_all(exp.Table):
        name = t.name.lower()
        if name in cte_names:
            continue
        schema = (t.db or "").lower()
        if schema not in ALLOWED_SCHEMAS or name not in ALLOWED_TABLES:
            hint = " (use customers_v for customer data)" if name == "customers" else ""
            raise SqlRejected(
                f"table {t.sql()} is not allowed{hint}. Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}"
            )
        tables.add(name)
    if not tables:
        raise SqlRejected("query must read from at least one allowed table")

    limit_applied = False
    limit = stmt.args.get("limit")
    current = None
    if limit is not None:
        try:
            current = int(limit.expression.this)
        except (AttributeError, TypeError, ValueError):
            current = None
    if current is None or current > max_rows:
        stmt = stmt.limit(max_rows, copy=True)
        limit_applied = True

    return GuardResult(sql=stmt.sql(dialect="postgres"), tables=tables, limit_applied=limit_applied)
