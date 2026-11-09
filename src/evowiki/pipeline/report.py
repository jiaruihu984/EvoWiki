from typing import Any, get_args

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator
from pydantic.json_schema import SkipJsonSchema


class StageReport(BaseModel):
    stage: str
    created: list[str] = Field(default_factory=list)
    flagged: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    llm_call_ids: list[str] = Field(default_factory=list)

    def merge(self, other: "StageReport") -> "StageReport":
        return StageReport(
            stage=self.stage,
            created=[*self.created, *other.created],
            flagged=[*self.flagged, *other.flagged],
            dropped=[*self.dropped, *other.dropped],
            llm_call_ids=[*self.llm_call_ids, *other.llm_call_ids],
        )


class InvalidItem(BaseModel):
    index: int
    error: str


def _summary(exc: ValidationError, limit: int = 200) -> str:
    text = "; ".join(f"{'.'.join(map(str, e['loc'])) or '<item>'}: {e['msg']}"
                     for e in exc.errors())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class LenientBatch(BaseModel):
    """LLM batch output whose list fields drop invalid elements instead of failing the whole
    batch. Rejections land in `invalid_items` (hidden from the provider schema, dumped only
    when non-empty so valid outputs keep their recorded shape) for the stage to report."""

    invalid_items: SkipJsonSchema[list[InvalidItem]] = Field(
        default_factory=list, exclude_if=lambda v: not v)

    @model_validator(mode="before")
    @classmethod
    def _drop_invalid_elements(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        invalid = list(data.get("invalid_items") or [])
        for name, field in cls.model_fields.items():
            raw = data.get(name)
            if name == "invalid_items" or not isinstance(raw, list):
                continue
            [element_type] = get_args(field.annotation)
            adapter = TypeAdapter(element_type)
            kept = []
            for i, element in enumerate(raw):
                try:
                    kept.append(adapter.validate_python(element))
                except ValidationError as exc:
                    invalid.append({"index": i, "error": _summary(exc)})
            data[name] = kept
        data["invalid_items"] = invalid
        return data

    def invalid_notes(self, stage: str) -> list[str]:
        return [f"{stage}: invalid item {i.index}: {i.error}" for i in self.invalid_items]
