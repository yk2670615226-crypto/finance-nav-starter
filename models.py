"""数据模型定义，包含应用配置、收支记录与用户信息。"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, Text, Boolean, DateTime, Index
from sqlalchemy.orm import declarative_base
from werkzeug.security import check_password_hash, generate_password_hash

Base = declarative_base()


class AppSettings(Base):
    """应用全局配置表，记录预算、启动锁与演示模式等全局参数。"""

    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=True, comment="用户 ID")
    monthly_budget = Column(Float, default=0.0, comment="月度预算")
    enable_lock = Column(Boolean, default=False, comment="是否开启启动锁")
    lock_password_hash = Column(String(256), nullable=True, comment="启动锁密码Hash")
    is_demo = Column(Boolean, default=False, comment="是否处于演示模式")

    def set_lock_password(self, password: str) -> None:
        """设置应用锁密码，仅保存哈希值。"""
        self.lock_password_hash = generate_password_hash(password)

    def check_lock_password(self, password: str) -> bool:
        """校验应用锁密码。"""
        if not self.lock_password_hash:
            return False
        return check_password_hash(self.lock_password_hash, password)


class Record(Base):
    """收支记录表，保存单条收入与支出流水。"""

    __tablename__ = "records"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=True, comment="用户 ID")
    ts = Column(
        DateTime, index=True, nullable=False, default=datetime.now, comment="交易时间"
    )
    amount = Column(Float, nullable=False, comment="金额")
    type = Column(String(16), index=True, nullable=False, comment="收支类型")
    category = Column(String(64), index=True, default="其他", comment="分类")
    note = Column(Text, default="", comment="备注")
    is_demo = Column(Boolean, default=False, nullable=False, comment="是否演示数据")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")

    def __repr__(self) -> str:
        return f"<Record id={self.id} ts={self.ts} amount={self.amount} type={self.type} cat={self.category}>"


Index("idx_records_type_ts", Record.type, Record.ts)


class User(Base):
    """用户表，记录登录账号与认证信息。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    uid = Column(
        String(64), unique=True, nullable=False, index=True, comment="登录 UID"
    )
    username = Column(String(128), nullable=False, comment="自定义用户名")
    password_hash = Column(String(256), nullable=False, comment="密码哈希")
    created_at = Column(
        DateTime, default=datetime.now, nullable=False, comment="注册时间"
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def __repr__(self) -> str:
        return f"<User id={self.id} uid={self.uid}>"
