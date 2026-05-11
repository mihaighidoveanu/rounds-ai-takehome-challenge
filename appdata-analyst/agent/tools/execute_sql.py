import json
import logging
import os

import sqlglot
import psycopg
from pydantic_ai import RunContext
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.deps import AgentDeps

logger = logging.getLogger(__name__)


def execute_sql(ctx: RunContext[AgentDeps], sql: str) -> dict:
    """Execute a read-only SQL SELECT query against the analytics database.
    Returns columns and rows on success, or an error dict on failure.
    Use this to answer questions about apps and daily_metrics.
    """
    if ctx.deps.db_pool is None:
        return {"ok": False, "error": "sql_error: DB pool not initialized"}

    statements = sqlglot.parse(sql, read="postgres")
    if len(statements) != 1 or type(statements[0]).__name__ != "Select":
        return {"ok": False, "error": "sql_invalid: only a single SELECT statement is allowed"}

    stmt = statements[0]
    canonical_sql = stmt.sql(dialect="postgres")

    timeout_ms = os.environ.get("SQL_STATEMENT_TIMEOUT_MS", "10000")
    hard_row_cap = int(os.environ.get("SQL_HARD_ROW_CAP", 50000))
    hard_result_bytes = int(os.environ.get("SQL_HARD_RESULT_BYTES", 5242880))

    columns: list = []
    rows_list: list = []

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        retry=retry_if_exception_type(psycopg.OperationalError),
        reraise=True,
    )
    def _run() -> tuple[list, list]:
        with ctx.deps.db_pool.connection(timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
                cur.execute(canonical_sql)
                rows = cur.fetchall()
                cols = [desc.name for desc in cur.description]
                return cols, [list(r) for r in rows]

    try:
        columns, rows_list = _run()
    except psycopg.errors.QueryCanceled:
        return {"ok": False, "error": "sql_timeout"}
    except psycopg.Error as e:
        return {"ok": False, "error": f"sql_error: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"sql_error: {e}"}

    hard_capped = False
    original_row_count = None

    if len(rows_list) > hard_row_cap or len(json.dumps(rows_list, default=str)) > hard_result_bytes:
        original_row_count = len(rows_list)
        rows_list = rows_list[:hard_row_cap]
        hard_capped = True

    tool_call_id = getattr(ctx, "tool_call_id", None)
    if tool_call_id is not None:
        ctx.deps.original_result_cache[tool_call_id] = {
            "columns": columns,
            "rows": rows_list,
        }

    result: dict = {
        "ok": True,
        "columns": columns,
        "rows": rows_list,
        "row_count": len(rows_list),
        "hard_capped": hard_capped,
    }
    if hard_capped:
        result["original_row_count"] = original_row_count

    return result
