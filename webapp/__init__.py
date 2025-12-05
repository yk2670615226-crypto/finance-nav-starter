import os
from flask import Flask
from flask_socketio import SocketIO
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from models import Base
from webapp.config import get_base_path, get_data_path

socketio = SocketIO(cors_allowed_origins="*")


def create_app() -> Flask:
    base_dir = get_base_path()
    data_dir = get_data_path()
    db_path = data_dir / "finance.db"

    template_dir = base_dir / "templates"
    static_dir = base_dir / "static"

    app = Flask(__name__, template_folder=str(template_dir), static_folder=str(static_dir))
    app.config["SECRET_KEY"] = os.urandom(24).hex()
    print(f"数据库路径: {db_path}")

    engine = create_engine(
        f"sqlite:///{db_path}?check_same_thread=False",
        poolclass=NullPool,
        future=True,
    )
    Base.metadata.create_all(engine)

    session_factory = scoped_session(sessionmaker(bind=engine))
    app.config["SessionFactory"] = session_factory
    app.config["DATA_DIR"] = data_dir
    app.config["BASE_DIR"] = base_dir

    from webapp.routes import bp as main_bp

    app.register_blueprint(main_bp)
    socketio.init_app(app)

    return app
