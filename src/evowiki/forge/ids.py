"""The forge's reserved scope name (spec §10). 'global' is the shared scope of the registry and
settings; a user id equal to it would read and write the shared scope. Migration 0068 refuses such
a user in the database; every forge entry point refuses it here too (fail closed, two places)."""
GLOBAL = "global"


def check_user(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id.strip() or user_id == GLOBAL:
        raise ValueError(f"not a valid forge user id: {user_id!r}")
    return user_id
