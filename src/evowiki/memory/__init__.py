"""Cross-session memory (spec the product specification):
a derived, rebuildable index over the session log. It never writes K; preferences and facts
about the learner live in the teaching spec and the profile, this package only points at them."""


def __getattr__(name: str):
    # lazy: `memory.contracts` is imported by modules that must not load the store / assistant
    if name == "MemoryPorts":
        from evowiki.memory.ports import MemoryPorts
        return MemoryPorts
    raise AttributeError(name)
