"""GET / PUT /wiki/settings: 每天最多复习几条, 生成内容的语言, 单页写作花费上限. Bounds are the schema's
(422 before this runs); a field the body does not send is left as it is."""

from datetime import datetime

from evowiki.deep.contracts import RefineSettings
from evowiki.wikiapp.events import LanguageSet, RefineSettingsSet, ReviewSettingsSet, WikiEvent
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Commit, Repo
from evowiki.wikiapp.views import settings_io
from evowiki.wikiapp.wiki_schemas import SettingsIO


def get_settings(repo: Repo, user_id: str) -> SettingsIO:
    state = repo.load(user_id).state
    return settings_io(state.review, state.language, state.refine)


def put_settings(repo: Repo, user_id: str, body: SettingsIO, *, now: datetime,
                 new_id: Ids) -> SettingsIO:
    """Writes only what the body sends and changed."""
    snap = repo.load(user_id)
    state = snap.state
    sent = body.model_fields_set
    bodies: list = []
    if "review_per_day" in sent and body.review_per_day != state.review.max_per_day:
        bodies.append(ReviewSettingsSet(settings=state.review.model_copy(
            update={"max_per_day": body.review_per_day})))
    if "language" in sent and body.language != state.language:
        bodies.append(LanguageSet(language=body.language))
    limit = RefineSettings(per_page_limit_usd=body.page_limit_usd)
    if "page_limit_usd" in sent and limit != state.refine:
        bodies.append(RefineSettingsSet(settings=limit))
    if bodies:
        snap = repo.commit(snap, Commit(wiki=[WikiEvent(event_id=new_id("wev"), ts=now, body=b)
                                              for b in bodies]))
    return settings_io(snap.state.review, snap.state.language, snap.state.refine)
