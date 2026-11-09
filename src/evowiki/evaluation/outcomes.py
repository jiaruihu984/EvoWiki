from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictBool


class QuizOutcome(BaseModel):
    """Observed quiz result (a channel-A label). `event_id` links it to its quiz_answer event."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    ts: AwareDatetime
    correct: StrictBool
    event_id: str
