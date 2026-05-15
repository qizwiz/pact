"""Demo file — contains intentional violations to show the pact call graph."""

import json
from typing import Optional


def load_config(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except:  # noqa — bare except for demo
        return {}


def process_response(response) -> Optional[str]:
    # llm_response_unguarded: no check before indexing choices
    return response.choices[0].message.content


def fetch_user(db, user_id: int):
    user = db.query("SELECT * FROM users WHERE id = ?", user_id).first()
    # optional_dereference: .first() can return None
    return user.name


def register(handlers: list = []):  # mutable_default_arg
    handlers.append("new")
    return handlers
