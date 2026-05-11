import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from agent.response_model import ProseBlock, TableBlock, ChartBlock, ButtonsBlock, AnalyticsResponse
from formatting.app_links import slack_app_link


def render_prose(block: ProseBlock) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": block.text}}]


def render_table(block: TableBlock) -> list[dict]:
    result: list[dict] = []

    # Header block
    result.append({"type": "header", "text": {"type": "plain_text", "text": block.title[:150]}})

    # Optional context block with store links
    if block.app_context:
        result.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": " | ".join(
                        slack_app_link(a.name, a.app_id, a.platform)
                        for a in block.app_context
                    ),
                }
            ],
        })

    # Build monospace text table, hiding "app_id" columns
    visible_indices = [i for i, col in enumerate(block.columns) if col != "app_id"]
    visible_columns = [block.columns[i] for i in visible_indices]

    display_rows = block.rows[:20]

    # Compute column widths
    col_widths = [len(col) for col in visible_columns]
    for row in display_rows:
        for j, i in enumerate(visible_indices):
            cell = str(row[i]) if i < len(row) else ""
            col_widths[j] = max(col_widths[j], len(cell))

    def format_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(col_widths[j]) for j, cell in enumerate(cells))

    header_row = format_row(visible_columns)
    separator = "  ".join("-" * col_widths[j] for j in range(len(visible_columns)))
    data_rows = "\n".join(
        format_row([str(row[i]) if i < len(row) else "" for i in visible_indices])
        for row in display_rows
    )

    table_text = "```\n" + header_row + "\n" + separator + "\n" + data_rows + "\n```"

    result.append({"type": "section", "text": {"type": "mrkdwn", "text": table_text}})

    return result


def render_buttons(block: ButtonsBlock) -> list[dict]:
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": btn.text[:75]},
                    "action_id": f"analytics_followup_{i}",
                    "value": btn.text[:2000],
                }
                for i, btn in enumerate(block.buttons)
            ],
        }
    ]


def render_chart_png(block: ChartBlock) -> bytes:
    fig, ax = plt.subplots(figsize=(10, 5))
    try:
        if block.chart_type in ("line", "area"):
            if block.series:
                for series in block.series:
                    x = block.x_values if block.x_values else range(len(series.y_values))
                    ax.plot(x, series.y_values, label=series.name)
                    if block.chart_type == "area":
                        ax.fill_between(x, series.y_values, alpha=0.3)
            else:
                x = block.x_values if block.x_values else range(len(block.y_values or []))
                ax.plot(x, block.y_values or [])

        elif block.chart_type == "bar":
            if block.series:
                for series in block.series:
                    x = block.x_values if block.x_values else range(len(series.y_values))
                    ax.bar(x, series.y_values, label=series.name)
            else:
                x = block.x_values if block.x_values else range(len(block.y_values or []))
                ax.bar(x, block.y_values or [])

        elif block.chart_type == "scatter":
            if block.series:
                for series in block.series:
                    x = block.x_values if block.x_values else range(len(series.y_values))
                    ax.scatter(x, series.y_values, label=series.name)
            else:
                x = block.x_values if block.x_values else range(len(block.y_values or []))
                ax.scatter(x, block.y_values or [])

        elif block.chart_type == "pie":
            ax.pie(block.y_values or [], labels=block.labels or [], autopct="%1.1f%%")

        elif block.chart_type == "hist":
            ax.hist(block.y_values or block.x_values or [])

        ax.set_title(block.title)
        if block.x_label:
            ax.set_xlabel(block.x_label)
        if block.y_label:
            ax.set_ylabel(block.y_label)
        if block.series and len(block.series) > 1:
            ax.legend()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        return buf.read()
    finally:
        plt.close(fig)


def render_component(comp) -> list[dict]:
    if comp.type == "prose":
        return render_prose(comp)
    elif comp.type == "table":
        return render_table(comp)
    elif comp.type == "buttons":
        return render_buttons(comp)
    # chart is handled upstream in composer.py via render_chart_png
    return []
