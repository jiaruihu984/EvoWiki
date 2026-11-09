"""A `teach.approval.Approvals` for the RSI tests: a fixed level per scope, the proposals it got,
and the keys the user rejected in the ledger."""

from evowiki.teach.approval import RSI, TEACHING_PARAMS


class FakeApprovals:
    def __init__(self, *, ask: bool = True, scopes=(TEACHING_PARAMS, RSI)):
        self.ask, self.scopes = ask, set(scopes)
        self.proposals: list[dict] = []
        self.rejected_keys: set[str] = set()

    def asks(self, user_id, scope):
        return self.ask and scope in self.scopes

    def propose(self, user_id, **kw):
        self.proposals.append({"user_id": user_id, **kw})

    def rejected(self, user_id, key):
        return key in self.rejected_keys


class Effect:
    """The ledger's effect stand-in: counts its runs."""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
