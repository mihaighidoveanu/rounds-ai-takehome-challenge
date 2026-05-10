# Rounds AI Analytics Slack Chatbot — Plan

All paths in this document are relative to `appdata-analyst/` unless prefixed with `db/` (the existing analytics SQL init lives at the repo root).

## Architecture

The agent runs in two logical phases per user query, with a third rendering step in the listener.

```
┌─────────────────────────────────────────────────────────┐
│ Phase 1: ReAct loop (data gathering)                    │
│   LLM → execute_sql → results → LLM → execute_sql → ... │
│   Loop continues until LLM has enough data              │
├─────────────────────────────────────────────────────────┤
│ Phase 2: Final LLM call (response composition)          │
│   After gathering enough data, do an LLM call to compose the final response; |
 |  pydantic-ai validates this response against AnalyticsResponse |
 | — an ordered list of components (prose / table / chart / buttons). │
│  
│           │
├─────────────────────────────────────────────────────────┤
│ Phase 3: Listener renders to Slack                      │
│   composer iterates components in order:                │
│     - chart  → files_upload_v2 (private), then append   │
│                an image block referencing the file_id   │
│     - prose / table / buttons → append blocks directly  │
│   Interim ack message is updated with final blocks;     │
│   continuations are threaded only if Slack's 50-block    │
│   limit is exceeded.                                    │
└─────────────────────────────────────────────────────────┘
```

Only `execute_sql` (and the existing `add_emoji_reaction`) are agent tools. `render_table` / `render_chart` / `render_buttons` are **not** tools — they're listener-side renderers that consume the structured output.

## Existing Skeleton — Keep as Is

| File | Status |
|---|---|
| `app.py`, `app_oauth.py` | `app.py` initializes DB pool + agent executor shutdown; `app_oauth.py` unchanged |
| `agent/agent.py` — `_PROVIDERS` list, `get_model()` runtime selection, `run_sync()` pattern | unchanged structure |
| `agent/deps.py` — existing fields | extend only |
| `agent/tools/emoji_reaction.py` | unchanged, kept in tool list |
| `thread_context/store.py` | extended (strip intermediate tool calls/returns on save; preserve the final structured-output result) |
| `listeners/actions/feedback_buttons.py`, `listeners/views/feedback_builder.py` | unchanged |
| `listeners/events/app_home_opened.py`, `assistant_thread_started.py` | unchanged |
| `pyproject.toml`, `requirements.txt` | extend |

`manifest.json` **is** modified: add the bot scope `files:write` for chart uploads. Re-install the Slack app after changing the manifest; otherwise `files_upload_v2` will fail with `missing_scope`.

## Structured Output Model

```python
# agent/response_model.py
from typing import Literal, Annotated
from pydantic import BaseModel, Field, model_validator

class ProseBlock(BaseModel):
    type: Literal["prose"]
    text: str  # Slack mrkdwn

class AppRef(BaseModel):
    name: str
    app_id: str
    platform: Literal["iOS", "Android"]

class TableBlock(BaseModel):
    type: Literal["table"]
    title: str
    columns: list[str]
    rows: list[list]
    app_context: list[AppRef] | None = None  # enables store-link enrichment

class ChartSeries(BaseModel):
    name: str
    y_values: list[float]

class ChartBlock(BaseModel):
    type: Literal["chart"]
    chart_type: Literal["line", "bar", "scatter", "pie", "hist", "area"]
    title: str
    x_values: list | None = None
    series: list[ChartSeries] | None = None  # preferred for multi-series
    y_values: list[float] | None = None      # single-series shortcut
    labels: list[str] | None = None          # pie slice labels
    x_label: str | None = None
    y_label: str | None = None

    @model_validator(mode="after")
    def _exclusive_series(self):
        if self.series and self.y_values:
            raise ValueError("provide either `series` or `y_values`, not both")
        if self.series and self.chart_type in ("pie", "hist"):
            raise ValueError(f"`series` is not supported for chart_type={self.chart_type}")
        return self

class Button(BaseModel):
    text: str  # also used as the follow-up query value

class ButtonsBlock(BaseModel):
    type: Literal["buttons"]
    buttons: list[Button]

ResponseComponent = Annotated[
    ProseBlock | TableBlock | ChartBlock | ButtonsBlock,
    Field(discriminator="type"),
]

class AnalyticsResponse(BaseModel):
    components: list[ResponseComponent]
    notes: list[str] | None = None  # surfaces caveats (e.g. compressed or hard-capped results)
```

## File-by-File Changes

### `docker-compose.yml` — add Langfuse stack

Pin Langfuse v2 (single Postgres + web container). Langfuse v2 is EOL relative to v3, but it is intentionally chosen here to keep the take-home stack small; Add a TODO in the Readme to migrate to v3. 

Use a non-conflicting host port for the Langfuse Postgres (`5433:5432`) to avoid collision with the analytics DB on `5432`. Langfuse web is reachable at `localhost:3000` when the app is run on the host via `python app.py`. Keys are generated via the Langfuse UI on first run and stored in `.env`.

### `pyproject.toml` / `requirements.txt`

Add:
- `psycopg[binary,pool]` — sync DB driver + pool
- `sqlglot` — SQL parsing/validation
- `tenacity` — retry helpers
- `langfuse` — tracing client
- `matplotlib` — chart rendering

Pin `pydantic-ai[anthropic,groq,openai]` to a known-good version before implementation, then verify the exact structured-output validation exception names against that installed version. The plan relies on `output_type=`, `history_processors=`, and the `(ctx, messages)` history-processor signature, so this dependency should not float.

(Slack Bolt and dotenv are already present.)

### `.env.sample` — additions

```
DATABASE_URL=postgresql://analyst_ro:analyst_ro@localhost:5432/analytics
SQL_STATEMENT_TIMEOUT_MS=10000
SQL_HARD_ROW_CAP=50000
SQL_HARD_RESULT_BYTES=5242880

LLM_TIMEOUT_S=60
CONTEXT_OVERFLOW_THRESHOLD_TOKENS=100000   # ~76% of a 131k window; triggers compression
COMPRESSION_MODEL=                         # optional override; defaults to active agent model
AGENT_WORKER_THREADS=1

LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=http://localhost:3000

```

(`GROQ_*` and `OPENAI_*` blocks remain; provider priority unchanged.)

### `db/init/00_role.sql` — NEW

Defense-in-depth read-only role used by `DATABASE_URL`. Granted `SELECT` on `apps` and `daily_metrics` only. Even if the SQL parser is bypassed, write attempts fail at the DB layer.

### `agent/deps.py` — add run fields

```python
db_pool: ConnectionPool | None = None  # psycopg sync pool
run_budget: RunBudget | None = None    # informational view of remaining context capacity
aggregate_cache: dict[str, dict] = Field(default_factory=dict)  # keyed by tool_call_id
original_result_cache: dict[str, dict] = Field(default_factory=dict)  # keyed by tool_call_id; full payloads for client-side subset rebuilds
compression_planner_failed: bool = False  # set when LLM compression planner errors; reset at start of run_agent
stale_summary: bool = False              # set when conversation summarizer falls back to cached digest; reset at start of run_agent
```

### `db/connection.py` — NEW (sync pool, matching `run_sync()`)

Module-level `ConnectionPool` with `create_pool(dsn)` / `get_pool()` / `close_pool()`, opened from `app.py` before the Bolt handler starts.

### `agent/tools/execute_sql.py` — NEW

Synchronous Pydantic-AI tool with the signature:

```python
def execute_sql(ctx: RunContext[AgentDeps], sql: str) -> dict
```

The existing `add_emoji_reaction` tool can remain async; Pydantic AI accepts a mixed sync/async tool list while the outer app continues using `run_sync()`.

Behavior:
- Parse with `sqlglot.parse(sql, read="postgres")`. Reject unless exactly one statement and it is a `Select`. Re-emit canonicalized SQL for execution.
- Acquire a connection with `ctx.deps.db_pool.connection(timeout=2)`.
- Set per-query `statement_timeout` from `SQL_STATEMENT_TIMEOUT_MS`.
- Fetch the **full** result set. The tool no longer does budget-driven row-tail truncation — right-sizing happens centrally in the history processor on the next model call (see `thread_context/summarizer.py`).
- Apply a pure-safety **hard cap** to prevent OOM on a runaway result: if the row count exceeds `SQL_HARD_ROW_CAP` (default 50_000) or the serialized payload exceeds `SQL_HARD_RESULT_BYTES` (default 5 MiB), trim the tail to the cap, set `hard_capped=True`, and record `original_row_count`. This is distinct from semantic compression — it's a runaway guard, not budget management. Cache the (capped) full payload on `ctx.deps.original_result_cache` keyed by `tool_call_id` so the compression planner can rebuild subsets client-side without re-querying.
- Retry only on `psycopg.OperationalError` (connection drops) via `tenacity`. Validation errors and query errors are not retried.
- On any failure, return a structured error to the LLM rather than raising:

```python
# success
{"ok": True,
 "columns": [...], "rows": [...], "row_count": N,
 "hard_capped": bool, "original_row_count": M}  # M only present when hard_capped
# failure
{"ok": False, "error":
   "sql_invalid: ..."
 | "sql_timeout"
 | "sql_error: ..."}
```

The LLM can self-correct on `ok: False` (rewrite the query) without crashing the agent loop. `hard_capped=True` is rare in normal use; semantic right-sizing for context fit is handled by the compression planner described below.

### `agent/context_window.py` — NEW

Centralizes context-window arithmetic so the compression planner and history processor can both reason about remaining capacity.

```python
MODEL_CONTEXT_WINDOW = {
    "anthropic:claude-opus-4-7":     200_000,
    "anthropic:claude-sonnet-4-6":   200_000,
    "anthropic:claude-haiku-4-5":    200_000,
    "groq:openai/gpt-oss-120b":      131_072,   # active default for this project
    "openai:gpt-4.1":                200_000,
    # ... extend as needed; default falls back to 131_000
}

def estimate_tokens(payload) -> int:
    """Cheap heuristic: serialize and divide. ~4 chars per token."""
    return ceil(len(json.dumps(payload, default=str)) / 4)

@dataclass
class RunBudget:
    model: str
    total_tokens: int       # refreshed before each LLM call; informational only
    headroom: int

    @property
    def limit(self) -> int:
        return MODEL_CONTEXT_WINDOW.get(self.model, 100_000)

    def remaining(self) -> int:
        return max(0, self.limit - self.headroom - self.total_tokens)

    def deficit(self) -> int:
        """Positive when current input exceeds the safe limit; used by the compression planner."""
        return max(0, self.headroom + self.total_tokens - self.limit)

    def refresh(self, current_messages) -> None:
        self.total_tokens = estimate_tokens(current_messages) + TOOLS_SCHEMA_OVERHEAD_TOKENS
```

Built once per agent run inside `run_agent`, attached to `AgentDeps.run_budget`, then refreshed by the history processor before each model call. `RunBudget` is **purely informational** — it no longer charges tool returns and is no longer consulted by `execute_sql` for sizing. Its sole role is to expose `remaining()` / `deficit()` to the compression planner so it knows how many tokens it must free. There is no `charge()` method; semantic right-sizing happens in the planner step, not at the tool layer.

### `agent/response_model.py` — NEW

The Pydantic models above.

### `agent/agent.py` — modifications

1. Replace `SYSTEM_PROMPT` with the analytics prompt. Sections:
   - **Schema** — `apps(app_id, name, platform)` and `daily_metrics(app_id, date, country, installs, in_app_revenue, ads_revenue, ua_cost)`. Note that `platform` lives on `apps`, so platform breakdowns require a JOIN.
   - **Derived metrics** — total revenue = `in_app_revenue + ads_revenue`; ROAS = revenue / `ua_cost`; etc.
   - **Time conventions** — assume "last month"/"this week" etc. against today's date.
   - **Response composition heuristics** — scalar → prose only; ranking ≥ 2 rows → table; time series ≥ 7 points → chart; ambiguous query → prose clarifying question + follow-up buttons for possible meanings
   - **App enrichment rule** — when querying apps, always SELECT `apps.app_id`, `apps.name`, `apps.platform` so the table renderer can produce store links via `app_context`.
   - **Compression / hard-cap rule** — if any tool return carries `compressed=True` or `hard_capped=True`, the result was reduced (semantically compressed by the planner, or clipped by the runaway-safety cap). You **must** add a partial-coverage note to `AnalyticsResponse.notes` describing what was reduced (e.g. "Some prior query results were summarized to fit context — totals may be approximate", or "First N of M rows shown — query was capped for safety"). Never present aggregates from a compressed/dropped/hard-capped result without the note. For `hard_capped=True` you should also try to refine with `GROUP BY` / tighter filters / smaller `LIMIT` to get a meaningful slice.
   - **Chart catalog** — which fields each chart_type needs (line/bar/scatter/area need x+y; pie needs labels+y_values; hist needs y_values only; multi-series uses `series` for line/bar/scatter/area only).
   - **Component ordering is honored verbatim** — charts render inline at the position emitted.
2. Add `output_type=AnalyticsResponse` to the `Agent(...)` constructor.
3. Add `execute_sql` to `tools=[...]` (keep `add_emoji_reaction`).
4. Add `history_processors=[compress_if_overflow]` so the compression check runs before every model call (initial + post-tool-return + final composition).
5. Bound the ReAct loop via the agent's iteration cap (e.g. `max_result_retries=6`).
6. Do **not** wrap the whole `agent.run_sync(...)` in tenacity. Retrying the entire ReAct run would re-issue every SQL call after a transient final-call failure. Rely on Pydantic AI/model-provider retry and fallback behavior for transient LLM failures; keep explicit tenacity only around the SQL connection-drop path.
7. Pass `model_settings={"timeout": LLM_TIMEOUT_S}`. If explicit model thinking is enabled later, increase `LLM_THINKING_HEADROOM_TOKENS` and include it in the RunBudget headroom.
8. `run_agent()` signature unchanged. Internally it (a) constructs a `RunBudget` for the active model, (b) attaches it to `AgentDeps.run_budget`, (c) calls `agent.run_sync(...)`. Summarization is no longer called from here — the agent's `history_processors` hook handles it on demand. Result `.output` is now an `AnalyticsResponse`.

### `thread_context/store.py` — extend

The existing store keeps `list[ModelMessage]` per `(channel_id, thread_ts)`. Two extensions:

- **Projection on save.** `set_history(...)` filters the message list before persistence:
  - Keep `ModelRequest` messages whose parts include `UserPromptPart`.
  - Keep `ModelResponse` messages whose parts include `TextPart`.
  - Find the final `final_result` tool-call entry that Pydantic AI generates for `output_type=AnalyticsResponse`, deserialize its arguments back into an `AnalyticsResponse`, and store it as a plain `TextPart` inside a `ModelResponse` containing the full structured JSON. Drop the `ToolCallPart`/`ToolReturnPart` pair entirely. This gives all providers a reliable plain assistant turn on follow-up calls — no separate compact-digest mechanism needed.
  - Drop all intermediate `execute_sql` `ToolCallPart` / `ToolReturnPart` entries. Those are scoped to one agent run; persisting them across turns bloats history with rows that are recoverable from the DB.

  ```python
  # In thread_context/store.py set_history():
  final_response_json = response.model_dump_json(indent=2)
  synthetic_text_message = ModelResponse(parts=[TextPart(content=final_response_json)])
  ```
- **Summary cache.** Add `get_summary(channel_id, thread_ts, prefix_count) -> str | None` and `set_summary(channel_id, thread_ts, prefix_count, summary)` for the summarizer's incremental cache. In-memory dict is fine for v1; same lifetime as the conversation store itself.

### `thread_context/summarizer.py` — NEW

Compresses conversation history when (and only when) it would otherwise overflow.

**Scope.** The summarizer operates on two different kinds of context. Cross-turn history is mostly user/assistant conversation, plus the preserved final structured-output answer. Intermediate tool messages are stripped at persistence time (see `thread_context/store.py`). In-flight messages from the current run can still contain `execute_sql` calls/returns, so the same processor can compress current-run tool returns if the prompt would otherwise overflow.

**Trigger — check before every LLM call.** Implemented as a Pydantic AI `history_processor` on the agent: a callable that runs immediately before every model request the agent makes — the first call, every continuation after a tool return, and (importantly) the final composition call. The largest input is typically that final call, after several `execute_sql` results have accumulated, so checking only there isn't enough; checking before every call ensures we catch the threshold trip wherever it happens.

The processor measures the **complete LLM input** = system prompt (lives as the first message) + the full in-flight message list (conversation + tool calls/returns from this run) + tools schema overhead. If that total exceeds `CONTEXT_OVERFLOW_THRESHOLD_TOKENS` (default 100k of the 131k window), it compresses the input before returning. Below threshold, the messages flow through verbatim.

```python
TOOLS_SCHEMA_OVERHEAD_TOKENS = 2_000   # rough, fixed constant for execute_sql + add_emoji_reaction descriptions

def compress_if_overflow(ctx: RunContext[AgentDeps], messages: list[ModelMessage]) -> list[ModelMessage]:
    if _total_input(messages) <= CONTEXT_OVERFLOW_THRESHOLD_TOKENS:
        ctx.deps.run_budget.refresh(messages)
        return messages

    # Step 1a: normal conversation-prefix summarization (keep HISTORY_KEEP_RECENT_TURNS verbatim)
    messages = _summarize_conversation_prefix(messages, keep_recent=HISTORY_KEEP_RECENT_TURNS)
    if _total_input(messages) <= CONTEXT_OVERFLOW_THRESHOLD_TOKENS:
        ctx.deps.run_budget.refresh(messages)
        return messages

    # Step 1b: full summarization — keep 0 turns verbatim
    messages = _summarize_conversation_prefix(messages, keep_recent=0)
    if _total_input(messages) <= CONTEXT_OVERFLOW_THRESHOLD_TOKENS:
        ctx.deps.run_budget.refresh(messages)
        return messages

    # Step 2: LLM compression planner — content-aware, conditioned on the latest user query
    try:
        plan = _run_compression_planner(messages, deps=ctx.deps)
        messages = _apply_compression_plan(messages, plan, deps=ctx.deps)
        if _total_input(messages) <= CONTEXT_OVERFLOW_THRESHOLD_TOKENS:
            ctx.deps.run_budget.refresh(messages)
            return messages
    except Exception:
        logger.warning("compression planner failed; falling through to deterministic step 3", exc_info=True)
        ctx.deps.compression_planner_failed = True

    # Step 3: deterministic fallback — oldest-first aggregate of any remaining (uncompressed) tool returns
    messages = _aggregate_old_tool_returns_iterative(messages, deps=ctx.deps)
    if _total_input(messages) <= CONTEXT_OVERFLOW_THRESHOLD_TOKENS:
        ctx.deps.run_budget.refresh(messages)
        return messages

    # Step 4: nothing left to compress — surface to user
    raise ContextExhausted()

def _total_input(messages) -> int:
    return estimate_tokens(messages) + TOOLS_SCHEMA_OVERHEAD_TOKENS
```

The order matters: conversation summarization runs first because (a) it's the cheapest source of compression (incremental, cached), and (b) it preserves user intent best. The LLM planner is the next escalation: it's content-aware — it sees the user's latest query alongside per-tool-call metadata and chooses *which* stored results to compress and *how*. The deterministic aggregate walk is the safety net for when the planner errors or its plan is insufficient. If everything is exhausted and the context is still over threshold (e.g. the system prompt alone is near the limit, or the most recent single SQL result is enormous), `compress_if_overflow` raises `ContextExhausted`.

Three trigger points are all covered by this same hook firing before every model call:
- **First-time threshold trip** at the run's initial call (a long-lived thread crossed 100k via accumulated history alone).
- **Mid-run after a tool call** when a large `execute_sql` result pushed cumulative tokens over threshold.
- **Right before the final composition call**, which is the typical worst case after multiple SQL results have piled up.

#### Step 2 — LLM compression planner

A separate LLM call (model = `COMPRESSION_MODEL` if set, else the active agent model) with a fresh context. It receives a capped JSON payload, not raw rows:

```json
{
  "user_query": "<latest user turn text>",
  "research_goal_summary": "<from cached digest if present, else first user turn>",
  "conversation_history": [
    {"role": "user" | "assistant", "content": "<text>"},
    ...
  ],
  "deficit_tokens": <int>,
  "tool_calls": [
    {
      "tool_call_id": "<id>",
      "sql": "<original SQL>",
      "columns": [{"name": "...", "type": "numeric|text|date|..."}],
      "row_count": <int>,
      "approx_tokens": <int>,
      "head_sample": [<up to 3 rows>],
      "tail_sample": [<up to 2 rows>],
      "already_compressed": <bool>
    }
  ]
}
```

`conversation_history` is built by walking the (already-step-1-compressed) in-flight messages and projecting `UserPromptPart` / `TextPart` content only — tool calls/returns are represented separately under `tool_calls`. The summarized prefix produced by step 1 appears as the first synthetic `user` entry (the `[earlier conversation summary] …` message), so the planner sees both the digest of older turns and the verbatim recent turns. With per-tool samples capped at `head 3 + tail 2` rows, the payload is naturally bounded — no application-level token cap is imposed on this call. The provider's own context-window error is the only ceiling, and we don't pre-empt it.

**Output — Pydantic-validated structured response.** Each in-flight return is targeted by exactly one action; missing entries default to `keep_full`.

```python
class RowSelector(BaseModel):
    kind: Literal["head", "tail", "top_n_by", "indices"]
    n: int | None = None                        # for head / tail / top_n_by
    by: str | None = None                       # column name for top_n_by
    direction: Literal["asc", "desc"] | None = None
    indices: list[int] | None = None            # for kind="indices"

class CompressionAction(BaseModel):
    tool_call_id: str
    op: Literal["keep_full", "subset", "aggregate", "drop"]
    keep_columns: list[str] | None = None       # required iff op == "subset"; None = keep all
    row_selector: RowSelector | None = None     # required iff op == "subset"; None = keep all rows

class CompressionPlan(BaseModel):
    actions: list[CompressionAction]
    rationale: str                              # short explanation, kept for tracing
```

Planner system prompt: *"You are choosing how to compress prior SQL results so the model can still answer the user's latest query. Free at least `deficit_tokens` tokens. Prefer `keep_full` for results clearly relevant to the latest query; prefer `subset` (small `n`) when only a slice matters; use `aggregate` for results whose row-level detail is irrelevant but counts/sums/ranges might be cited; use `drop` only for clearly off-topic results."*

#### Step 2 application — `_apply_compression_plan(messages, plan, deps)`

For each action, mutate the matching `ToolReturnPart` in place (preserving `tool_call_id` exactly — provider validates the call/return linkage):

- `keep_full` — no change.
- `subset` — applied **client-side from `deps.original_result_cache`** (no DB round trip). Column projection is a dict filter; `row_selector` dispatch:
  - `head` / `tail` / `indices`: pure list slicing.
  - `top_n_by`: `sorted(rows, key=lambda r: r[col], reverse=(direction=="desc"))[:n]`. If the column is missing or non-orderable, coerce to `head`.
  - If the original payload is not in cache (e.g. a return from an earlier run rehydrated from history), coerce that single action to `aggregate`.
- `aggregate` — reuse the existing primitive: re-run `SELECT <type-aware aggs> FROM (<original SQL>) sub` against the read-only pool with `sqlglot` validation and `statement_timeout`. Cached on `deps.aggregate_cache` keyed by `tool_call_id`.
- `drop` — replace with `{ok: True, compressed: True, compression_kind: "drop", original_row_count: N, sql: "<short>", note: "Earlier query result omitted to free context. Re-run if needed."}`.

Every replacement carries `compressed=True`, `compression_kind`, and `original_row_count`. Post-application payloads are cached by `tool_call_id` so subsequent history-processor firings within the same run skip already-compressed returns (idempotency).

#### Step 3 — deterministic fallback

Oldest-first walk applying the existing aggregate primitive (see `_aggregate_old_tool_returns_iterative` below) to each remaining uncompressed return until under threshold. This is the same mechanism the prior plan used as its primary step 2; it is now reserved for planner failures or under-shoots. Aggregate replacements are still cached by `tool_call_id` so step 3 doesn't re-run aggregates the planner already produced.

#### Failure handling

| Where | Failure | Recovery |
|---|---|---|
| Planner LLM | Timeout / schema-invalid / network | Log WARNING, set `deps.compression_planner_failed = True`, fall through to step 3 |
| Step 3 walk | Aggregate SQL fails for a specific return | Replace that one with a `drop` stub; continue |
| All steps exhausted | Still over threshold | Raise `ContextExhausted`; existing listener path surfaces user-facing message |
| `top_n_by` | Column missing / non-orderable | Coerce to `head` |
| Subset reconstruction | Original payload not in cache | Coerce that single action to `aggregate` |

`agent/job.py` appends a stale-compression caveat to `response.notes` when `deps.compression_planner_failed` is set, parallel to today's stale-summary block.

**Conversation summarization (`_summarize_conversation_prefix`)**:
- Operates only on user/assistant message parts (`UserPromptPart`, `TextPart`); leaves tool calls/returns from the current run untouched.
- `HISTORY_KEEP_RECENT_TURNS` (default 4) is a **soft preference**: keep those turns verbatim on the normal first pass (step 1a). If still over threshold, re-run with `keep_recent=0` (step 1b) to maximize compression.
- Keep the last `keep_recent` user-turns (paired with their assistant responses) verbatim as `recent_verbatim`. Everything older is the `to_summarize` prefix.
- Call `get_model()` (the same provider/model the main agent uses) with a preservation-focused prompt:

  > You are compressing a Slack analytics conversation so it fits in the model's context window. Preserve everything that a future turn might need to answer correctly. **Always keep:**
  > - The user's research goal / original question
  > - Every filter or scope the user established: apps, app_ids, date ranges, countries, platforms (iOS/Android), metrics (revenue/installs/UA/ROAS), granularities (daily/weekly/monthly)
  > - Every numeric answer or finding given to the user, with the filters it was scoped to
  > - Any clarification or correction the user made ("actually I meant Q1 not Q2")
  > - User preferences expressed (chart types, units, ordering)
  > - Any unanswered or follow-up question still in flight
  >
  > **Drop:**
  > - Pleasantries, acknowledgments, restatements
  > - The model's explanation of how it arrived at an answer (the data/SQL is recoverable; the conclusion is what matters)
  > - Repetition across turns
  >
  > Output a structured digest of ≤ 600 tokens. Use bullet sub-sections: `Goal`, `Filters in effect`, `Findings`, `User preferences`, `Open questions`. If a section has no content, omit it.

- Replace the prefix with a single synthetic user message: `"[earlier conversation summary]\n<digest>"`. Return `[summary_message, *recent_verbatim]`.

**Caching — incremental per prefix.** Keyed on `(channel_id, thread_ts, prefix_message_count)` in `thread_context.store`. When a new turn shifts the boundary, we summarize *incrementally*: feed `previous cached digest + newly-aged-out turn` to the summarizer rather than re-summarizing the full prefix. This bounds summarizer cost to O(turn) regardless of thread length.

**Aggregate primitive (`_aggregate_old_tool_returns_iterative`)** — shared by the step-3 deterministic fallback and as a coerce-target by the step-2 planner when `subset` can't be reconstructed. A one-line digest discards too much; instead we **re-run the SQL wrapped in an aggregate** and substitute that back:
- Walk the in-flight messages and find `ToolReturnPart` entries (this run's `execute_sql` results), oldest-first. Skip entries already marked `compressed=True` (idempotency).
- For each `ToolReturnPart` to be aggregated, look up the original SQL on its matching `ToolCallPart` and the column types from the recorded result.
- Preserve the original `tool_call_id` exactly when replacing `ToolReturnPart` content; provider APIs validate the call/return linkage.
- Cache aggregate replacements on `AgentDeps.aggregate_cache`, keyed by `tool_call_id`, so repeated history-processor firings don't re-run the same aggregate SQL.
- Build an aggregate query of the form `SELECT <aggs> FROM (<original SQL>) sub` where `<aggs>` is generated per column type:
  - **numeric** (int / float / `numeric`): `SUM`, `AVG`, `MIN`, `MAX`
  - **date / timestamp**: `MIN`, `MAX`
  - **text / id**: `COUNT(DISTINCT)` plus a small array sample (`array_agg(DISTINCT col ORDER BY col LIMIT 5)`)
  - plus `COUNT(*) AS row_count` always
- Execute the aggregate against the read-only pool with the same `statement_timeout` and `sqlglot` validation as a normal `execute_sql` call.
- Replace the original `ToolReturnPart` content with `{ok: True, compressed: True, compression_kind: "aggregate", aggregated_from: "<short SQL>", original_row_count: N, aggregate: {...}, note: "rows replaced with aggregates to free context; query the data again for row-level detail"}`.
- Leave `ToolCallPart` entries themselves verbatim — they're small (just SQL) and let the model see what it asked for.
- If the aggregate query itself fails (e.g., the original SQL contained constructs that can't be wrapped — DDL-shaped things sqlglot already blocks, but window functions over CTEs can be tricky), substitute a `drop` stub (`compressed=True, compression_kind="drop"`) and log at `WARNING`.

**Cost note.** This costs at most one extra DB round trip per compacted return per run because aggregate replacements are cached by `tool_call_id`. Acceptable because compression only fires when conversation summarization wasn't enough — i.e., on heavy runs where the user is doing something genuinely big.

**Failure mode.** If the summarizer call fails (timeout, rate limit, etc.), fall back to passing `[previous cached digest if any, *recent_verbatim]` — i.e., we accept a slightly stale summary rather than crashing. Logged at `WARNING`. The agent run continues, and a flag (`deps.stale_summary = True`) is set so the listener can append a caveat to `AnalyticsResponse.notes` before posting: "Some earlier conversation context could not be refreshed — this response may not reflect all prior filters or findings. If the answer seems off, try restating your question with the relevant context." The flag is reset at the start of each `run_agent` call. If both compression steps are fully exhausted and the context is still over threshold, `ContextExhausted` is raised; the listener catches it and surfaces a user-facing message (see listener section).

### `formatting/app_links.py` — NEW

```python
def app_store_url(app_id: str, platform: str) -> str:
    if platform == "iOS":
        return f"https://apps.apple.com/app/id{app_id}"
    return f"https://play.google.com/store/apps/details?id={app_id}"

def slack_app_link(name: str, app_id: str, platform: str) -> str:
    return f"<{app_store_url(app_id, platform)}|{name}>"
```

### `formatting/renderers.py` — NEW

Pure functions, no Slack I/O on the rendering paths:

- `render_prose(block) -> Block` — section block with mrkdwn.
- `render_table(block) -> list[Block]` — header block + optional context block of store links + section block with monospace fenced table. The `app_id` column is hidden from the rendered table even though it must be returned by the SQL.
- `render_buttons(block) -> Block` — actions block built from `slack_sdk.models.blocks` (matches `feedback_builder.py` style). Each button uses `action_id="analytics_followup"` and `value=button.text`.
- `render_chart_png(block) -> bytes` — matplotlib dispatch on `chart_type`:
  - `line`, `bar`, `scatter`, `area`: iterate `series` if present, else use `y_values`. Add `ax.legend()` when multi-series.
  - `pie` → `ax.pie(y_values, labels=labels)`
  - `hist` → `ax.hist(y_values)` (or `x_values` if `y_values` is None)
  - Returns PNG bytes; `matplotlib.use("Agg")` at import.

If chart rendering raises, the composer catches and substitutes a `ProseBlock` ("⚠️ Chart unavailable: <title>") so the rest of the response still posts.

### `formatting/composer.py` — NEW (active poster, inline charts)

Charts are inlined into a **single** `chat_postMessage` by uploading the PNG privately (no channel share) and referencing the resulting `file_id` in an `image` block via `slack_file`.

```python
def post_response(client, channel, thread_ts, response, feedback_blocks, replace_ts=None):
    blocks: list[dict] = []
    for comp in response.components:
        if comp.type == "chart":
            try:
                png = render_chart_png(comp)
                upload = client.files_upload_v2(
                    content=png,
                    filename=f"{comp.title}.png",
                    title=comp.title,
                    # no channel arg → uploaded but not shared as its own message
                )
                file_id = upload["file"]["id"]
                blocks.append({
                    "type": "image",
                    "slack_file": {"id": file_id},
                    "alt_text": comp.title,
                    "title": {"type": "plain_text", "text": comp.title[:150]},
                })
            except Exception:
                blocks.extend(render_prose(ProseBlock(
                    type="prose", text=f":warning: Chart unavailable: {comp.title}"
                )))
        else:
            blocks.extend(render_component(comp))
    # always render notes (compression, hard-cap, caveats) as a visually distinct context block
    if response.notes:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "\n".join(f":warning: _{n}_" for n in response.notes)}],
        })
    blocks.extend(feedback_blocks)
    # Slack caps blocks at 50/message; if exceeded, post the overflow as a continuation
    _post_with_continuation(client, channel, thread_ts, blocks, replace_ts=replace_ts)
```

Notes:
- Bolt's `files_upload_v2` calls `files.getUploadURLExternal` → `files.completeUploadExternal`. Without `channel`/`channel_id`, the file is uploaded but not posted as its own message. If the SDK injects a default, fall back to calling the two endpoints directly.
- Image-block `slack_file` reference is the way Slack supports inline images that aren't from a public URL — the bot must own the file (it does) and the workspace audience can view it.
- `ensure_compression_note(response, messages)` lives here. It scans `result.all_messages()` for tool returns where `compressed=True` or `hard_capped=True`; if no existing note mentions partial/reduced data, it appends a generic warning note. Prefer adding a boolean helper internally rather than brittle substring checks scattered through listeners.
- The 50-blocks-per-message limit is handled by `_post_with_continuation`:
  - First message is posted with up to 50 blocks.
  - Continuations are posted as thread replies with a short leading context block like `_continued_`.
  - Chunk only at component boundaries. Do not split a chart image block from its title/caption or a table header from its table body.
  - Feedback buttons are appended only to the final continuation message.
- Title is clamped to 150 chars to stay under Slack's `plain_text` limits.

### `observability/langfuse.py` — NEW

`trace_agent_call(user_id, input_text, thread_ts)` sync context manager (handlers wrap `run_sync()`). On enter creates a trace; on exit updates output + latency + flushes. Reads `LANGFUSE_HOST` so it points at the local container or cloud. On listener-level failures, set `level="ERROR"` and `status_message=...`.

### `agent/executor.py` — NEW

Offload long-running agent work from Bolt listener threads. `agent.run_sync` can take 15-30 s, so the event handler should acknowledge visibly and return quickly instead of occupying the Socket Mode worker.

```python
_EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("AGENT_WORKER_THREADS", "1")))
_SEMAPHORE = threading.BoundedSemaphore(int(os.getenv("AGENT_WORKER_THREADS", "1")))

def submit_agent_job(fn, *args, **kwargs):
    if not _SEMAPHORE.acquire(blocking=False):
        raise AgentQueueFull()
    future = _EXECUTOR.submit(fn, *args, **kwargs)
    future.add_done_callback(lambda _: _SEMAPHORE.release())
    return future
```

On shutdown, `app.py` registers an `atexit` hook that calls `_EXECUTOR.shutdown(wait=False, cancel_futures=False)` after closing the DB pool. If the queue is full, the listener updates/posts a short "I'm at capacity; try again in a moment" message.

### `listeners/events/app_mentioned.py` & `message.py` — modifications

Replace the existing `say_stream(...)` path with an interim acknowledgment plus a background job:

1. Immediately post an interim threaded message such as `:mag: Looking that up...`; capture its `ts`.
2. Submit a background job via `submit_agent_job(...)`.
3. The job runs the full trace + agent + composer flow.
4. On success, upload any charts, then update the interim message in place with the final blocks when the response fits in one Slack message. If the response requires continuations, update the interim message with the first chunk and post continuation chunks in the same thread.
5. On failure, update the interim message with the timeout/validation/unexpected-error text.

The background job calls `run_agent_job(text, deps, history, interim_ts, client, logger)` from `agent/job.py` — that shared function owns the trace → `run_agent` → stale-summary caveat → stale-compression caveat → `ensure_compression_note` → `post_response` → save history → error-handling flow. Per-handler setup (payload parsing, interim ack, `AgentDeps` construction) stays in each listener since that part is legitimately different.

`STRUCTURED_OUTPUT_VALIDATION_ERROR` is a local alias determined after pinning/verifying the installed Pydantic AI version (commonly `UnexpectedModelBehavior` for exhausted output-validation retries). The streaming path goes away — the response is synthesized in one shot, then rendered atomically into the interim message. `set_status` calls remain for assistant-panel/DM flows; explicitly clear status (`set_status(status="")`) before posting if it does not auto-clear.

### `agent/job.py` — NEW (shared agent job)

A single shared function used by both the event-listener background job and the follow-up button handler. This avoids the trace → `run_agent` → `ensure_compression_note` → `post_response` → save history → error-handling block being duplicated across handlers.

```python
def run_agent_job(text, deps, history, interim_ts, client, logger):
    with trace_agent_call(deps.user_id, text, deps.thread_ts) as trace:
        try:
            result = run_agent(text, deps, message_history=history)
            response: AnalyticsResponse = result.output
            if deps.stale_summary:
                response.notes = (response.notes or []) + [
                    "Some earlier conversation context could not be refreshed — "
                    "this response may not reflect all prior filters or findings. "
                    "If the answer seems off, try restating your question with the relevant context."
                ]
            if deps.compression_planner_failed:
                response.notes = (response.notes or []) + [
                    "Context compression fell back to a coarse strategy for this turn — "
                    "some earlier query results may have been aggregated or dropped less precisely than usual. "
                    "If the answer seems off, narrow the question or start a fresh thread."
                ]
            ensure_compression_note(response, result.all_messages())
            post_response(
                client, deps.channel_id, deps.thread_ts,
                response, feedback_blocks=build_feedback_blocks(),
                replace_ts=interim_ts,
            )
            conversation_store.set_history(deps.channel_id, deps.thread_ts, result.all_messages())
            trace.update(output=response.model_dump())
        except LLMTimeout:
            client.chat_update(channel=deps.channel_id, ts=interim_ts, text=":hourglass: That query took too long. Try narrowing the date range or asking for fewer apps.")
            trace.update(level="ERROR", status_message="llm_timeout")
        except ContextExhausted:
            client.chat_update(channel=deps.channel_id, ts=interim_ts, text=":warning: This conversation has grown too large for the model's context window. Please start a new thread and try your question there.")
            trace.update(level="ERROR", status_message="context_exhausted")
        except STRUCTURED_OUTPUT_VALIDATION_ERROR:
            client.chat_update(channel=deps.channel_id, ts=interim_ts, text=":warning: I couldn't structure a response for that question - try rephrasing.")
            trace.update(level="ERROR", status_message="validation")
        except Exception as e:
            logger.exception(...)
            client.chat_update(channel=deps.channel_id, ts=interim_ts, text=":warning: Something went wrong; the error was logged. Try again.")
            trace.update(level="ERROR", status_message=str(e)[:200])
```

### `listeners/actions/analytics_followup.py` — NEW

Handles clicks on follow-up buttons emitted in `ButtonsBlock`. The button's text **is** the follow-up query. Per-handler setup (payload parsing, interim message, `AgentDeps` construction) stays here; the shared job body lives in `agent/job.py`.

```python
ACTION_ID = "analytics_followup"

def handle_analytics_followup(ack, body, client, context, logger):
    ack()
    value = body["actions"][0]["value"]                      # button text == query
    channel_id = body["channel"]["id"]
    thread_ts  = body["message"].get("thread_ts") or body["message"]["ts"]
    user_id    = body["user"]["id"]
    interim = client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=":mag: Looking that up...")
    history = conversation_store.get_history(channel_id, thread_ts)
    deps = AgentDeps(
        client=client, user_id=user_id, channel_id=channel_id,
        thread_ts=thread_ts, message_ts=interim["ts"],
        user_token=context.user_token, db_pool=get_pool(),
    )
    submit_agent_job(run_agent_job, value, deps, history, interim["ts"], client, logger)
```

Register in `listeners/actions/__init__.py` alongside the existing feedback action.

### `app.py` — additions

Before `SocketModeHandler(...).start()`, call `create_pool(os.environ["DATABASE_URL"])`. Register `atexit` handlers for `close_pool()` and `shutdown_agent_executor()`.

### `README.md` — extend with a "Run this solution" section

Append a new section at the end of the existing README (don't disturb the upstream-template content). The section should be a copy-pasteable runbook:

1. **Prerequisites.** Docker + Docker Compose, Python 3.11+, a Slack app set up via the existing manifest, an API key for at least one provider (Groq / Anthropic / OpenAI).
2. **One-time setup.**
   - Add `files:write` to the Slack bot scopes and re-install the app to the workspace.
   - `cp .env.sample .env` and fill in `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `DATABASE_URL`, the chosen `*_API_KEY` + `*_MODEL`, and the `LANGFUSE_*` keys (instructions on how to generate them via the Langfuse UI on first run, or how to swap to Langfuse Cloud).
   - `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
3. **Boot infrastructure.** `docker compose up -d` brings up the analytics Postgres (port 5432) + Langfuse stack (port 3000). Wait for healthchecks. Note the analytics seed loads automatically from `db/init/`.
4. **Generate Langfuse keys.** Open `localhost:3000`, create an organization/project, copy the public/secret keys back into `.env`.
5. **Run the agent.** `python app.py`. Confirm the connection log lines for Slack + DB pool.
6. **Smoke test.** Cite the verification queries from `PLAN.md` ("how many apps do we have?" / "top 5 apps by total revenue last month" / etc.) so a reviewer can walk through them in Slack.
7. **Observability.** Point at the Langfuse UI for trace inspection; describe what each trace contains (input, full `AnalyticsResponse` JSON, tool calls, latency).
8. **Troubleshooting.**
   - "No AI provider configured" → set one `*_API_KEY` + matching `*_MODEL`.
   - 403 on Slack → manifest scopes drift; reinstall to workspace.
   - `missing_scope` on chart upload → confirm `files:write` is present and the app was re-installed after the manifest change.
   - DB connection errors → `docker compose ps` and inspect logs.
9. **Architecture pointer.** Single line at the top of the section: "See `PLAN.md` for the full design and rationale." Keep the runbook focused on doing rather than explaining.

## Reliability: Timeouts, Retries, and Graceful Failure

Applied consistently at three layers; retries are surgical (transient classes only).

### LLM layer (`agent/agent.py`)
- `model_settings={"timeout": LLM_TIMEOUT_S}` (default 60 s).
- Iteration cap on the agent (e.g. `max_result_retries=6`) bounds the ReAct loop.
- No tenacity wrapper around the whole `run_sync` call; whole-run retries would duplicate SQL work. Use Pydantic AI/provider retry and fallback behavior for transient model errors.
- Pydantic validation errors are handled through Pydantic AI's output-validation retry path. Pin the package and alias the final exhausted-validation exception locally after verifying the installed version.

### SQL layer (`agent/tools/execute_sql.py`)
- `pool.connection(timeout=2)` for connection acquisition.
- `SET LOCAL statement_timeout = '<SQL_STATEMENT_TIMEOUT_MS>ms'` per query.
- `tenacity` retry only on `psycopg.OperationalError` (connection drops). Syntax / data errors return `{ok: False, error: ...}` so the LLM can rewrite.
- DB-side defense: read-only role, plus `sqlglot` parser rejecting non-`Select` statements.

### Listener layer
- Event/action handlers post an interim threaded acknowledgment immediately, then offload the full agent + render flow to `agent/executor.py`.
- Worker jobs use one try/except around the full agent + render flow.
- Three user-facing failure surfaces: timeout, validation, unexpected — each updates the interim message with a friendly message.
- A bounded worker pool (`AGENT_WORKER_THREADS`, default 1) caps concurrent ReAct loops and DB load. The design remains concurrency-capable; the default will increase after implementing "TODO — Implement Concurrent Safety Features - gating `AGENT_WORKER_THREADS` above 1".
- Langfuse trace marked `level="ERROR"` with a short status message on failure.

## Context Window Protection

We protect the model's context window centrally in the history processor rather than at the SQL tool. `execute_sql` returns the **full** result set (subject only to a coarse runaway-safety cap); semantic right-sizing happens on the next model call, conditioned on the user's actual question.

### Coordinated compression on threshold, never drop blindly

The bloat in any given LLM call comes from two places: cross-turn history (user/assistant turns piling up over a long thread) and within-run tool returns (large `execute_sql` results accumulated since the run began). Right before the **final composition call** is typically when the input is at its largest, since by then several SQL results may have stacked up. We check before every LLM call (using a Pydantic AI `history_processor`) and compress in ordered steps when the threshold is exceeded:

1. **Summarize the older user/assistant conversation prefix** (two passes). First pass keeps `HISTORY_KEEP_RECENT_TURNS` (default 4) verbatim; if still over threshold, a second pass summarizes the full conversation (`keep_recent=0`). Cheapest, highest semantic value; preserves user intent. Cached and incremental.
2. **LLM compression planner — content-aware** (only triggered if step 1 wasn't enough). A separate LLM call receives the full conversation history (post step-1 summarization), the user's latest query, the token deficit, and per-tool-call metadata (sql, columns, row_count, head/tail samples) for every in-flight `execute_sql` return. It returns a `CompressionPlan` of `keep_full` / `subset` / `aggregate` / `drop` actions — one per tool call. The planner is intent-aware: a result the user just referenced gets `keep_full` or `subset`; off-topic earlier scans get `aggregate` or `drop`. `subset` is rebuilt client-side from the cached original payload (no DB round-trip); `aggregate` reuses the SQL re-run primitive (cached by `tool_call_id`); `drop` replaces with a small stub.
3. **Deterministic fallback** (oldest-first aggregate walk) if the planner errors or its plan doesn't free enough tokens. Same primitive as step 2's `aggregate` op, applied uniformly. Sets `deps.compression_planner_failed = True`, which surfaces as a caveat in `AnalyticsResponse.notes`.
4. **Raise `ContextExhausted`** if every step is exhausted and the context is still over threshold. The listener catches this and posts a user-facing message: "This conversation has grown too large for the model's context window. Please start a new thread."

`thread_context/summarizer.py` owns all four steps. The DB round-trips for step-2 `aggregate` ops and step-3 fall back to the same read-only pool the agent uses via `AgentDeps`, with the same `sqlglot` validation and `statement_timeout`. Everything is gated behind a single threshold check on the **complete LLM input** = system prompt + in-flight messages + tools schema overhead.

Three invariants hold throughout:
- **Cross-turn history keeps the answer, not intermediate data pulls.** Intermediate `execute_sql` tool calls and returns are stripped at persistence time (see `thread_context/store.py`), while the final structured-output result is preserved/digested so follow-ups have an antecedent. Within-run compression handles the *current* turn's tool returns.
- **The choice of *what* to compress is content-aware.** Instead of mechanically dropping the oldest or trimming the tail of the latest result, the planner judges relevance against the user's actual question. A result that's old but pivotal stays; a recent off-topic scan can be dropped.
- **Recent material is spared first, but not at the cost of a provider-level failure.** `HISTORY_KEEP_RECENT_TURNS` (default 4) is a soft preference. When the context is still over threshold after the normal passes, compression escalates to cover everything rather than letting the LLM call fail with a 400 error.

### Tool results — full results in, planner trims later

Each agent run constructs a `RunBudget` (in `agent/context_window.py`) seeded from the active model's context window minus a headroom reserve (`LLM_RESPONSE_HEADROOM_TOKENS`, default 8k, plus `LLM_THINKING_HEADROOM_TOKENS` if explicit thinking is enabled). The `RunBudget` is **purely informational** — the history processor calls `refresh()` to update its view of consumed tokens and reads `deficit()` to tell the compression planner how many tokens it must free. The tool never charges the budget itself.

When `execute_sql` runs, it fetches the full result and returns it verbatim, with one exception: a pure-safety hard cap to prevent OOM on a runaway result. If the row count exceeds `SQL_HARD_ROW_CAP` (default 50_000) or the serialized payload exceeds `SQL_HARD_RESULT_BYTES` (default 5 MiB), the tail is trimmed and `hard_capped=True` is set on the return. This is distinct from semantic compression — it's a fail-safe, not budget management. The (capped) full payload is cached on `deps.original_result_cache` keyed by `tool_call_id` so the compression planner can rebuild `subset` views client-side without re-querying.

The system prompt requires the LLM to inspect `compressed` / `hard_capped` flags on tool returns and **always** add a clear partial-coverage note to `AnalyticsResponse.notes` if either is present. For `hard_capped` the model should also try to refine with `GROUP BY` / filters / a smaller `LIMIT`. For `compressed` it should describe what kind of reduction happened (aggregated, subsetted, dropped).

The listener enforces this as a safety net: after the agent run, `ensure_compression_note` scans `result.all_messages()` for any tool return where `compressed=True` or `hard_capped=True`. If found and `notes` doesn't already mention partial/reduced data, it appends a generic warning. The composer always renders `notes` as a context block in the message, styled to be visually distinct from the prose answer.

## Data Flow

```
Slack event
    │
    ▼
listener (app_mentioned | message | analytics_followup)
    │  build AgentDeps(client, user_id, channel_id, thread_ts, message_ts, user_token, db_pool)
    │  load history from thread_context.store
    │  post interim threaded ack and submit bounded background job
    │
    ▼
agent/executor.py worker
    │
    ▼
trace_agent_call(...)  ─►  Langfuse (local or cloud; local assumes python app.py runs on host)
    │
    ▼
run_agent(text, deps, history)
    │  agent.run_sync(text, deps=deps, model=get_model(), message_history=history,
    │                 model_settings={"timeout": LLM_TIMEOUT_S})
    │
    ├── ReAct loop ──► execute_sql (× N) ─► sqlglot validate ─► db_pool ─► rows back to LLM
    │                  add_emoji_reaction ─► reactions_add (kept from skeleton)
    │
    └── Final LLM call ──► AnalyticsResponse(components=[...], notes=[...])
    │
post_response(client, channel, thread_ts, response, feedback_blocks, replace_ts=interim_ts)
    │
    ├── for each chart: render_chart_png → files_upload_v2 (private) → image block w/ slack_file
    └── prose / table / buttons → blocks appended in component order
    │
    ▼
chat_update(interim message, first blocks chunk); optional threaded continuations if >50 blocks
    │
    ▼
store.save(history); trace.update(output)
```

## Implementation Phase Order

1. `docker-compose.yml` — add Langfuse v2 stack on a non-conflicting port; verify `localhost:3000` is reachable; generate keys.
2. `manifest.json` — add bot scope `files:write`; note workspace reinstall requirement.
3. `pyproject.toml` / `requirements.txt` — add `psycopg[binary,pool]`, `sqlglot`, `tenacity`, `langfuse`, `matplotlib`; pin `pydantic-ai[anthropic,groq,openai]`.
4. `.env.sample` — add `DATABASE_URL`, `SQL_STATEMENT_TIMEOUT_MS`, `SQL_HARD_ROW_CAP`, `SQL_HARD_RESULT_BYTES`, `LLM_TIMEOUT_S`, `LLM_*_HEADROOM_TOKENS`, `CONTEXT_OVERFLOW_THRESHOLD_TOKENS`, `COMPRESSION_MODEL`, `AGENT_WORKER_THREADS`, `LANGFUSE_*`, `ANTHROPIC_*`.
5. `db/init/00_role.sql` — read-only role.
6. `db/connection.py` — sync pool lifecycle.
7. `agent/deps.py` — add `db_pool` field, run-local budget, `aggregate_cache`, `original_result_cache`, `compression_planner_failed`, `stale_summary` flags.
8. `agent/response_model.py` — Pydantic models incl. multi-series chart + notes.
9. `agent/context_window.py` — `MODEL_CONTEXT_WINDOW`, `estimate_tokens`, `RunBudget` (informational view: `remaining()` / `deficit()` / `refresh()`; no `charge()`).
10. `thread_context/store.py` extend — preserve final structured result/digest, strip intermediate tool messages on save, add summary cache helpers.
11. `thread_context/summarizer.py` — `(ctx, messages)` history-processor: step-1 conversation-prefix summarization (LLM-backed, cached, incremental) → step-2 LLM compression planner (`CompressionPlan` of `keep_full`/`subset`/`aggregate`/`drop` actions, applied via `_apply_compression_plan`) → step-3 deterministic aggregate fallback → step-4 `ContextExhausted`. Aggregate cache + original-result cache both keyed by `tool_call_id`; planner failure flagged on deps.
12. `agent/tools/execute_sql.py` — sqlglot validation, `{ok: bool}` shape, statement timeout, retries, full-result return with `SQL_HARD_ROW_CAP` / `SQL_HARD_RESULT_BYTES` runaway-safety cap; populate `deps.original_result_cache`.
13. `formatting/app_links.py`.
14. `formatting/renderers.py` — prose/table/buttons → blocks; chart → PNG bytes; multi-series support.
15. `formatting/composer.py` — `post_response` with inline `slack_file` image blocks, `replace_ts`, continuation chunking, `ensure_compression_note` (scans for `compressed=True` or `hard_capped=True`), notes rendered as warning context block.
16. `agent/agent.py` — analytics system prompt (compression / hard-cap rule); `output_type=AnalyticsResponse`; `execute_sql` in tools; `history_processors=[compress_if_overflow]`; `RunBudget` setup in `run_agent`; LLM timeout; iteration cap; no whole-run tenacity retry.
17. `agent/executor.py` — bounded `ThreadPoolExecutor`, capacity handling, shutdown hook.
18. `observability/langfuse.py` — trace context manager; emit a `compression_planner` span with the JSON payload + plan output.
19. `agent/job.py` — shared `run_agent_job` function: trace → `run_agent` → stale-summary caveat → stale-compression caveat → `ensure_compression_note` → `post_response` → save history → structured error handling.
20. `listeners/events/app_mentioned.py` + `message.py` — interim ack + submit background job via `agent/job.py`.
21. `listeners/actions/analytics_followup.py` + register in `listeners/actions/__init__.py` — interim ack + submit background job via `agent/job.py`.
22. `app.py` — pool init + executor shutdown + atexit close.
23. Tests — focused unit tests for SQL validation + hard-cap behavior, chart/table rendering, history persistence/filtering, compression thresholding (step-1 summarization, step-2 planner + `_apply_compression_plan` dispatch, step-3 deterministic fallback, planner-failure flag), composer continuation chunking, and follow-up action error handling.
24. End-to-end smoke test against the seeded DB and local/cloud Langfuse.
25. `README.md` — append "Run this solution" runbook (prerequisites, env setup, Slack reinstall after `files:write`, `docker compose up`, Langfuse key generation, `python app.py`, smoke-test queries, observability pointer, troubleshooting).

## Test Plan

Add focused tests rather than broad snapshot tests:

- `execute_sql`: rejects multiple statements and non-`SELECT`; returns structured `{ok: False}` on SQL errors; applies statement timeout; populates `deps.original_result_cache`; `SQL_HARD_ROW_CAP` / `SQL_HARD_RESULT_BYTES` clip oversized results with `hard_capped=True` and `original_row_count`.
- `thread_context/store.py`: strips intermediate `execute_sql` tool calls/returns but preserves the final structured-output answer and/or its digest for follow-up context.
- `thread_context/summarizer.py`: threshold no-op path; step-1a normal summarization path; step-1b full summarization path (`keep_recent=0`); step-2 LLM planner — `_apply_compression_plan` produces the expected modified `ToolReturnPart` for each of `keep_full`/`subset`/`aggregate`/`drop`; `tool_call_id` preserved; `subset` dispatch over `head`/`tail`/`top_n_by`/`indices` with missing-column → `head` fallback; planner failure path sets `deps.compression_planner_failed` and step-3 deterministic walk runs; step-3 oldest-first aggregate walk stops early; aggregate cache reuse by `tool_call_id`; `ContextExhausted` raised when all steps exhausted.
- `formatting/renderers.py`: table rendering hides `app_id` while keeping app links; chart rendering returns non-empty PNG bytes for each supported chart type.
- `formatting/composer.py`: `ensure_compression_note` injects a note when any tool return has `compressed=True` or `hard_capped=True` and `notes` is silent; `replace_ts` update behavior; 50-block continuation chunking at component boundaries; feedback buttons only on the final chunk.
- `listeners/actions/analytics_followup.py`: posts interim ack, submits background job, and mirrors typed-query error handling.

## Verification Queries

After `docker compose up -d` and `python app.py`:

| Query | Expected components order |
|---|---|
| "how many apps do we have?" | `[prose]` |
| "top 5 apps by total revenue last month" | `[table(app_context populated), prose, buttons]` |
| "chart daily revenue for Paint since Jan 2025" | `[prose, chart(line)]` (or any explicit order) |
| "iOS vs Android revenue over last quarter" | `[chart(line, multi-series), prose]` (requires `apps JOIN daily_metrics`) |
| "split of installs by platform last week" | `[chart(pie), prose]` (also requires the JOIN) |
| "biggest UA spend swings Jan vs Dec 2024" | `[table, prose, buttons]` |
| "which apps are doing well?" | `[prose]` (clarifying question) |
| Follow-up "and iOS only?" in thread | filtered, history-aware response |
| Follow-up button click | re-runs agent with the button text as the new query |

Langfuse UI shows each trace with input, output (full `AnalyticsResponse` JSON), tool calls, latency, and error level on failures.
