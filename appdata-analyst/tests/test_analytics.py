import json
from dataclasses import dataclass
from unittest.mock import MagicMock, patch
import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, TextPart, ToolCallPart, ToolReturnPart
from agent.deps import AgentDeps
from agent.response_model import AnalyticsResponse, ProseBlock, TableBlock, ChartBlock, ChartSeries, AppRef, ButtonsBlock
from agent.tools.execute_sql import execute_sql
from formatting.renderers import render_table, render_chart_png
from formatting.composer import ensure_compression_note
from thread_context.store import ConversationStore


@dataclass
class FakeCtx:
    deps: AgentDeps
    tool_call_id: str = "test-call-id"


def _make_deps(db_pool=None) -> AgentDeps:
    return AgentDeps(
        client=MagicMock(),
        user_id="U1",
        channel_id="C1",
        thread_ts="ts1",
        message_ts="ts2",
        db_pool=db_pool,
    )


# ---------------------------------------------------------------------------
# 1. execute_sql validation tests
# ---------------------------------------------------------------------------

def test_rejects_multiple_statements():
    """Multiple statements must be rejected with sql_invalid error."""
    deps = _make_deps(db_pool=MagicMock())
    ctx = FakeCtx(deps=deps)
    result = execute_sql(ctx, "SELECT 1; SELECT 2")
    assert result["ok"] is False
    assert "sql_invalid" in result["error"]


def test_rejects_non_select():
    """Non-SELECT statements must be rejected with sql_invalid error."""
    deps = _make_deps(db_pool=MagicMock())
    ctx = FakeCtx(deps=deps)
    result = execute_sql(ctx, "INSERT INTO apps VALUES (1)")
    assert result["ok"] is False
    assert "sql_invalid" in result["error"]


def test_returns_ok_false_when_no_pool():
    """When db_pool is None the tool must return a DB pool error immediately."""
    deps = _make_deps(db_pool=None)
    ctx = FakeCtx(deps=deps)
    result = execute_sql(ctx, "SELECT 1")
    assert result["ok"] is False
    assert result["error"] == "sql_error: DB pool not initialized"


# ---------------------------------------------------------------------------
# 2. Chart rendering tests (real matplotlib, no mocking)
# ---------------------------------------------------------------------------

def test_render_line_chart_returns_bytes():
    block = ChartBlock(
        type="chart",
        chart_type="line",
        title="Test",
        x_values=["a", "b", "c"],
        y_values=[1.0, 2.0, 3.0],
    )
    result = render_chart_png(block)
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_render_pie_chart_returns_bytes():
    block = ChartBlock(
        type="chart",
        chart_type="pie",
        title="Pie",
        labels=["A", "B"],
        y_values=[10.0, 20.0],
    )
    result = render_chart_png(block)
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_render_bar_chart_multi_series():
    block = ChartBlock(
        type="chart",
        chart_type="bar",
        title="Multi",
        x_values=["Jan", "Feb"],
        series=[
            ChartSeries(name="iOS", y_values=[1.0, 2.0]),
            ChartSeries(name="Android", y_values=[3.0, 4.0]),
        ],
    )
    result = render_chart_png(block)
    assert isinstance(result, bytes)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# 3. Table rendering tests
# ---------------------------------------------------------------------------

def test_render_table_hides_app_id():
    """The app_id column header must not appear in the rendered table text."""
    block = TableBlock(
        type="table",
        title="T",
        columns=["app_id", "name", "revenue"],
        rows=[["id1", "App1", 100]],
    )
    result = render_table(block)

    # Find the section block that contains the monospace table
    section_blocks = [b for b in result if b["type"] == "section"]
    assert len(section_blocks) == 1
    table_text = section_blocks[0]["text"]["text"]

    assert "app_id" not in table_text
    assert "id1" not in table_text  # the actual id value should also be absent


def test_render_table_with_app_context():
    """When app_context is provided, a context block with a store link must be present."""
    block = TableBlock(
        type="table",
        title="Apps",
        columns=["name", "revenue"],
        rows=[["MyApp", 500]],
        app_context=[AppRef(name="MyApp", app_id="123456789", platform="iOS")],
    )
    result = render_table(block)

    context_blocks = [b for b in result if b["type"] == "context"]
    assert len(context_blocks) == 1

    # The context block must contain a store link for the app
    element_texts = [
        elem["text"]
        for elem in context_blocks[0]["elements"]
        if elem.get("type") == "mrkdwn"
    ]
    combined = " ".join(element_texts)
    assert "apps.apple.com" in combined
    assert "123456789" in combined


# ---------------------------------------------------------------------------
# 4. ensure_compression_note tests
# ---------------------------------------------------------------------------

def _tool_return_message(hard_capped: bool, original_row_count: int = 100) -> ModelRequest:
    """Build a ModelRequest containing a ToolReturnPart that signals truncation."""
    payload = {
        "ok": True,
        "hard_capped": hard_capped,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "original_row_count": original_row_count,
    }
    return ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="execute_sql",
                content=json.dumps(payload),
                tool_call_id="c1",
            )
        ]
    )


def test_compression_note_added_when_hard_capped():
    """A compression note must be appended when a ToolReturnPart signals hard_capped=True."""
    messages = [_tool_return_message(hard_capped=True)]
    response = AnalyticsResponse(components=[ProseBlock(type="prose", text="hello")])
    ensure_compression_note(response, messages)
    assert response.notes is not None
    assert len(response.notes) > 0


def test_compression_note_not_added_when_already_present():
    """If a compression note is already in response.notes it must not be duplicated."""
    existing_note = "Some query results were summarized to fit context — totals may be approximate."
    messages = [_tool_return_message(hard_capped=True)]
    response = AnalyticsResponse(
        components=[ProseBlock(type="prose", text="hello")],
        notes=[existing_note],
    )
    ensure_compression_note(response, messages)
    assert len(response.notes) == 1


# ---------------------------------------------------------------------------
# 5. store.py projection tests
# ---------------------------------------------------------------------------

def _build_message_list():
    """Build a mixed message list with user prompt, tool call, and text parts."""
    return [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[ToolCallPart(tool_name="execute_sql", args="{}", tool_call_id="c1")]),
        ModelResponse(parts=[TextPart(content="hello")]),
    ]


def test_set_history_strips_tool_calls():
    """After round-tripping through the store, no ToolCallPart should survive."""
    store = ConversationStore()
    messages = _build_message_list()
    store.set_history("C", "T", messages)
    retrieved = store.get_history("C", "T")
    assert retrieved is not None

    all_parts = [part for msg in retrieved for part in msg.parts]
    tool_call_parts = [p for p in all_parts if isinstance(p, ToolCallPart)]
    assert len(tool_call_parts) == 0


def test_set_history_keeps_user_and_text():
    """After projection, UserPromptPart and TextPart must both be present."""
    store = ConversationStore()
    messages = _build_message_list()
    store.set_history("C", "T", messages)
    retrieved = store.get_history("C", "T")
    assert retrieved is not None

    all_parts = [part for msg in retrieved for part in msg.parts]
    assert any(isinstance(p, UserPromptPart) for p in all_parts)
    assert any(isinstance(p, TextPart) for p in all_parts)
