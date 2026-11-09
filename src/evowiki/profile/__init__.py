"""The learner profile (reset plan 2026-10-27 item 5): one plain-Chinese note about the learner,
`EvoWiki/我.md` in the vault, sent with every model call made for him.

After DeepTutor's layered memory (https://github.com/HKUDS/DeepTutor, Apache-2.0: a trace, L2
facts, an L3 profile kept as one readable document); the idea only, no code copied.

- `note`: the note's sections, its text, and merging the learner's edit of it (his edits win).
- `derive`: the lines the server writes from what it already knows (goals, progress, K, the
  preferences he stated, what recent chats left open).
- `context`: `context_for`, the ONE function that picks the sections a model call gets.
- `store` / `service`: the canonical copy on the server; the gateway reads it per call."""
