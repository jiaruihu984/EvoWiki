from datetime import datetime
from math import exp, prod, sqrt

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from evowiki.belief.theta import ThetaInfer
from evowiki.timeutil import days_between


class Belief(BaseModel):
    """Belief about one concept: Beta(alpha, beta) mastery + exponential forgetting."""

    model_config = ConfigDict(frozen=True)

    alpha: float = Field(gt=0.0)
    beta: float = Field(gt=0.0)
    stability: float = Field(gt=0.0)
    last_recall_ts: AwareDatetime | None
    evidence_count: int = 0

    @property
    def mastery(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def uncertainty(self) -> float:
        a, b = self.alpha, self.beta
        n = a + b
        return sqrt(a * b / (n * n * (n + 1.0)))

    def recall(self, now: datetime) -> float:
        if self.last_recall_ts is None:
            return 1.0
        dt = max(0.0, days_between(self.last_recall_ts, now))
        return exp(-dt / self.stability)


def predict_correct(beliefs: list[Belief], now: datetime, theta: ThetaInfer) -> float:
    q = prod(b.mastery * b.recall(now) for b in beliefs)
    return (1.0 - theta.slip) * q + theta.guess * (1.0 - q)
