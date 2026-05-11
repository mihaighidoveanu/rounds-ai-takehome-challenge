from typing import Literal, Annotated

from pydantic import BaseModel, Field, model_validator


class ProseBlock(BaseModel):
    type: Literal["prose"]
    text: str


class AppRef(BaseModel):
    name: str
    app_id: str
    platform: Literal["iOS", "Android"]


class TableBlock(BaseModel):
    type: Literal["table"]
    title: str
    columns: list[str]
    rows: list[list]
    app_context: list[AppRef] | None = None


class ChartSeries(BaseModel):
    name: str
    y_values: list[float]


class ChartBlock(BaseModel):
    type: Literal["chart"]
    chart_type: Literal["line", "bar", "scatter", "pie", "hist", "area"]
    title: str
    x_values: list | None = None
    series: list[ChartSeries] | None = None
    y_values: list[float] | None = None
    labels: list[str] | None = None
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
    text: str


class ButtonsBlock(BaseModel):
    type: Literal["buttons"]
    buttons: list[Button]


ResponseComponent = Annotated[
    ProseBlock | TableBlock | ChartBlock | ButtonsBlock,
    Field(discriminator="type"),
]


class AnalyticsResponse(BaseModel):
    components: list[ResponseComponent]
    notes: list[str] | None = None
