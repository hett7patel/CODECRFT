"""SentinelMesh Streamlit launcher."""

import os as sm_main_os
import pathlib as sm_main_pathlib

import streamlit as sm_main_st
from dotenv import load_dotenv as sm_main_load_dotenv

from agent import sm_run_case as sm_main_run_case
from backend import SMBackend as sm_main_backend_class
from demo_content import SM_DEMO_CONTENT as sm_main_demo_content
from ui import sm_render_app as sm_main_render_app


def sm_main_get_backend() -> sm_main_backend_class:
    """Create and start the single cached backend used by the app session."""
    sm_main_load_dotenv()
    # Streamlit Community Cloud provides secrets through st.secrets rather than
    # a checked-in .env. Copy only the expected settings into the process
    # environment so agent.py can read them at live-decision time.
    try:
        sm_main_secrets = sm_main_st.secrets
    except Exception:
        sm_main_secrets = {}
    for sm_main_key in ("GEMINI_API_KEY", "GEMINI_MODEL", "SM_DB_PATH"):
        if not sm_main_os.getenv(sm_main_key):
            try:
                sm_main_value = sm_main_secrets.get(sm_main_key)
            except Exception:
                sm_main_value = None
            if sm_main_value is not None:
                sm_main_os.environ[sm_main_key] = str(sm_main_value)
    configured_path = sm_main_os.getenv("SM_DB_PATH", "").strip()
    database_path = configured_path or str(
        sm_main_pathlib.Path(__file__).resolve().with_name("sentinelmesh.sqlite3")
    )
    backend = sm_main_backend_class(database_path, sm_main_demo_content)
    backend.start()
    return backend


@sm_main_st.cache_resource

def sm_main_cached_backend():
    return sm_main_get_backend()


def sm_bootstrap() -> None:
    """Configure Streamlit, obtain the backend, and render the application."""
    sm_main_st.set_page_config(
        page_title="SentinelMesh",
        page_icon="🛡️",
        layout="wide",
    )
    backend = sm_main_cached_backend()
    sm_main_render_app(backend, sm_main_run_case, sm_main_demo_content)


if __name__ == "__main__":
    sm_bootstrap()
