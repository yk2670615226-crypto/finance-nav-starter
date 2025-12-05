"""Flask 应用初始化与数据库 Schema 管理。"""

import os
import platform
import secrets

from flask import Flask
from flask_socketio import SocketIO
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from models import AppSettings, Base, Record, User
from webapp.config import get_base_path, get_data_path

socketio = SocketIO(cors_allowed_origins="*")


def load_or_create_secret_key(data_dir) -> str:
    """生成或加载应用的密钥文件，确保重启后会话保持。"""
    key_path = data_dir / "secret.key"
    if key_path.exists():
        existing = key_path.read_text().strip()
        if existing:
            return existing

    key = secrets.token_hex(24)
    key_path.write_text(key)
    return key


def create_app() -> Flask:
    """构建 Flask 应用并初始化数据库与蓝图。"""
    base_dir = get_base_path()
    data_dir = get_data_path()
    db_path = data_dir / "finance.db"

    template_dir = base_dir / "templates"
    static_dir = base_dir / "static"

    app = Flask(
        __name__, template_folder=str(template_dir), static_folder=str(static_dir)
    )
    app.config["SECRET_KEY"] = load_or_create_secret_key(data_dir)
    print(f"数据库路径: {db_path}")

    engine = create_engine(
        f"sqlite:///{db_path}?check_same_thread=False",
        poolclass=NullPool,
        future=True,
    )

    def ensure_schema() -> None:
        """创建或升级数据库表结构，保证旧版本数据可用。"""
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()

        Base.metadata.create_all(engine)

        with engine.begin() as conn:
            if "records" in existing_tables:
                record_cols = {c["name"] for c in inspector.get_columns("records")}
                if "user_id" not in record_cols:
                    conn.execute(text("ALTER TABLE records ADD COLUMN user_id INTEGER"))
                if "is_demo" not in record_cols:
                    conn.execute(
                        text(
                            "ALTER TABLE records ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT 0"
                        )
                    )

            if "app_settings" in existing_tables:
                setting_cols = {
                    c["name"] for c in inspector.get_columns("app_settings")
                }
                if "user_id" not in setting_cols:
                    conn.execute(
                        text("ALTER TABLE app_settings ADD COLUMN user_id INTEGER")
                    )

    ensure_schema()

    session_factory = scoped_session(sessionmaker(bind=engine))
    app.config["SessionFactory"] = session_factory
    app.config["DATA_DIR"] = data_dir
    app.config["BASE_DIR"] = base_dir

    from webapp.routes import bp as main_bp

    app.register_blueprint(main_bp)
    socketio.init_app(app)

    return app
