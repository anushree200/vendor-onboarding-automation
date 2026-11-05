import os
from typing import Any

from dotenv import load_dotenv


load_dotenv()


def get_setting(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value:
        return value

    try:
        import streamlit as st

        secret: Any = st.secrets.get(name, default)
    except Exception:
        return default
    return str(secret) if secret else default
