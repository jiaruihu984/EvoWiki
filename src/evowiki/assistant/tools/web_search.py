"""web_search: titles, links and snippets from a search provider (Claude Code / Codex WebSearch:
a result list only; reading a page is fetch_url's job). One real provider (Tavily, key from
TAVILY_API_KEY); an unset key is an explicit error, never a silent empty result."""

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from evowiki.agent.tool_helpers import (
    SEARCH_CHARS,
    TITLE_CHARS,
    clip,
    fail,
    run_guarded,
    spec,
)
from evowiki.agent.tools import Source, ToolContext, ToolResult

TAVILY_ENV = "TAVILY_API_KEY"
TAVILY_URL = "https://api.tavily.com/search"
HIT_SNIPPET_CHARS = 300
URL_CHARS = 2000              # longer links are dropped, so one hit block always fits SEARCH_CHARS
_MAX_RESPONSE_BYTES = 1_000_000

DESCRIPTION = ("联网搜索，返回标题、链接和摘要（最多 8 条）。需要最新信息、具体出处，或用户要你"
               "「查一下」时用；回答里用到的结果要列出链接。要读某个结果的正文再用 fetch_url。")

PostJson = Callable[[str, dict[str, str], dict[str, Any], float], Any]


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    url: str
    snippet: str = ""


class SearchError(Exception):
    pass


class SearchProvider(Protocol):
    def search(self, query: str, limit: int) -> Sequence[SearchHit]: ...


def clean_hit(hit: object) -> SearchHit | None:
    """A hit safe to show, or None: needs a title and an http(s) link of sane length; title and
    snippet are whitespace-collapsed and clipped. Applied to every provider's output."""
    if not isinstance(hit, SearchHit):
        return None
    title, url = " ".join(hit.title.split()), hit.url.strip()
    if not title or not url.startswith(("http://", "https://")) or len(url) > URL_CHARS:
        return None
    return SearchHit(title=clip(title, TITLE_CHARS), url=url,
                     snippet=clip(" ".join(hit.snippet.split()), HIT_SNIPPET_CHARS))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The request carries the API key, so it must never be re-sent to wherever a 3xx points;
    returning None makes urllib raise the 3xx as an HTTPError instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect())


def _urllib_post(url: str, headers: dict[str, str], body: dict[str, Any],
                 timeout: float) -> Any:
    """POST JSON, parse the JSON reply. Every failure becomes a SearchError whose message names
    only the failure kind, never request headers or exception text (which could hold the key)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers,
                                 method="POST")
    try:
        with _build_opener().open(req, timeout=timeout) as resp:
            return json.loads(resp.read(_MAX_RESPONSE_BYTES).decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SearchError(f"搜索服务返回 HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
        raise SearchError(f"连不上搜索服务：{type(e).__name__}") from None
    except ValueError:
        raise SearchError("搜索服务返回的不是 JSON") from None


class TavilyProvider:
    def __init__(self, api_key: str | None, *, post: PostJson | None = None,
                 timeout: float = 10.0):
        self._key, self._post, self._timeout = api_key, post or _urllib_post, timeout

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "TavilyProvider":
        return cls(env.get(TAVILY_ENV) or None)

    def search(self, query: str, limit: int) -> Sequence[SearchHit]:
        if not self._key:
            raise SearchError(f"未配置 {TAVILY_ENV}，联网搜索不可用")
        data = self._post(TAVILY_URL,
                          {"Authorization": f"Bearer {self._key}",
                           "Content-Type": "application/json"},
                          {"query": query, "max_results": limit, "search_depth": "basic"},
                          self._timeout)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise SearchError("搜索服务返回格式不对")
        hits = (clean_hit(SearchHit(title=str(r.get("title") or ""), url=str(r.get("url") or ""),
                                    snippet=str(r.get("content") or "")))
                for r in results if isinstance(r, dict))
        return [h for h in hits if h is not None][:limit]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=5, ge=1, le=8, description="最多返回几条，1–8")


class WebSearchTool:
    def __init__(self, provider: SearchProvider):
        self._provider = provider
        self.spec = spec("web_search", DESCRIPTION, Args, "network")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return run_guarded(Args, args, self._run)

    def _run(self, a: Args) -> ToolResult:
        try:
            raw = list(self._provider.search(a.query, a.limit))
        except SearchError as e:
            return fail(str(e))
        hits = [h for h in map(clean_hit, raw) if h is not None][:a.limit]
        if not hits:
            return ToolResult(content="没有搜到结果")
        blocks: list[str] = []
        shown: list[SearchHit] = []
        used = 0
        for i, h in enumerate(hits, 1):
            block = f"[{i}] {h.title}\n{h.url}\n{h.snippet}"
            cost = len(block) + (2 if blocks else 0)
            if used + cost > SEARCH_CHARS:
                break                       # a hit is cited only if the model actually saw it
            blocks.append(block)
            shown.append(h)
            used += cost
        return ToolResult(content="\n\n".join(blocks),
                          sources=tuple(Source(title=h.title, url=h.url) for h in shown))
