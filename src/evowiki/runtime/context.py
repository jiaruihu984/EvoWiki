from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool

from evowiki.belief.theta import ThetaInfer
from evowiki.content.loaded import LoadedContent
from evowiki.content.segments import SegmentIndex
from evowiki.render.theta import ThetaRender
from evowiki.runtime.policy import SCHEMA_VERSION, PolicyBundle
from evowiki.schema import ContentGraph
from evowiki.store.db import fetch_one
from evowiki.store.policies import register_policy


@dataclass(frozen=True)
class RuntimeContext:
    content: LoadedContent
    policy: PolicyBundle

    @property
    def graph(self) -> ContentGraph:
        return self.content.graph

    @property
    def segments(self) -> SegmentIndex:
        return self.content.segments

    @property
    def theta_infer(self) -> ThetaInfer:
        return self.policy.theta_infer

    @property
    def theta_rep(self) -> ThetaRender:
        return self.policy.theta_rep


def prepare_runtime(pool: ConnectionPool, content: LoadedContent, policy: PolicyBundle,
                    now: datetime) -> RuntimeContext:
    """The served content (it must be imported) and policy (registered)."""
    with pool.connection() as conn:
        if fetch_one(conn, "select 1 from content_snapshot where id = %s",
                     (content.version_id,)) is None:
            raise RuntimeError(f"content {content.version_id} is not imported; import it first")
        register_policy(conn, policy.version_id, policy.document(), SCHEMA_VERSION, now)
    return RuntimeContext(content, policy)
