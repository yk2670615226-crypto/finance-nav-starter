import threading
from contextlib import contextmanager
from typing import Generator

from flask import current_app
from sqlalchemy.orm import Session as OrmSession


db_write_lock = threading.RLock()


@contextmanager
def get_db_session(app=None) -> Generator[OrmSession, None, None]:
    """统一的 Session 上下文管理器，确保使用后关闭。"""
    flask_app = app or current_app
    session_factory = flask_app.config.get("SessionFactory")
    if session_factory is None:
        raise RuntimeError("SessionFactory 未初始化")

    session = session_factory()
    try:
        yield session
    finally:
        session.close()
