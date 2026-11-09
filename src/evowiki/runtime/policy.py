import hashlib
import json
from dataclasses import dataclass
from typing import Any

from evowiki.belief.theta import ThetaInfer
from evowiki.render.theta import ThetaRender

SCHEMA_VERSION = "theta-v1"


@dataclass(frozen=True)
class PolicyBundle:
    """θ_infer + θ_rep render parameters, identified by a content hash (plan decision 4)."""

    theta_infer: ThetaInfer
    theta_rep: ThetaRender
    version_id: str

    @staticmethod
    def _doc(ti: ThetaInfer, tr: ThetaRender) -> dict[str, Any]:
        return {"theta_infer": ti.model_dump(mode="json"),
                "theta_render": tr.model_dump(mode="json")}

    @classmethod
    def of(cls, ti: ThetaInfer, tr: ThetaRender) -> "PolicyBundle":
        blob = json.dumps(cls._doc(ti, tr), sort_keys=True, separators=(",", ":"))
        return cls(ti, tr, "pv_" + hashlib.sha256(blob.encode()).hexdigest()[:12])

    @classmethod
    def default(cls) -> "PolicyBundle":
        return cls.of(ThetaInfer.default(), ThetaRender.default())

    def document(self) -> dict[str, Any]:
        return self._doc(self.theta_infer, self.theta_rep)
