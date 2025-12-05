from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, Text, Boolean, DateTime, Index
from sqlalchemy.orm import declarative_base
from werkzeug.security import generate_password_hash, check_password_hash

Base = declarative_base()


class AppSettings(Base):
    """
    应用全局配置表（单用户模式，通常仅一行记录）。
    """
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    # 关联用户
    user_id = Column(Integer, index=True, nullable=True, comment="用户 ID")
    # 月度预算金额
    monthly_budget = Column(Float, default=0.0, comment="月度预算")
    
    # 应用启动锁相关配置
    enable_lock = Column(Boolean, default=False, comment="是否开启启动锁")
    lock_password_hash = Column(String(256), nullable=True, comment="启动锁密码Hash")
    
    # 演示模式标记
    is_demo = Column(Boolean, default=False, comment="是否处于演示模式")

    def set_lock_password(self, password: str) -> None:
        """设置应用锁密码（仅保存哈希值）。"""
        self.lock_password_hash = generate_password_hash(password)

    def check_lock_password(self, password: str) -> bool:
        """校验应用锁密码。"""
        if not self.lock_password_hash:
            return False
        return check_password_hash(self.lock_password_hash, password)


class Record(Base):
    """
    收支记录表。
    """
    __tablename__ = "records"

    id = Column(Integer, primary_key=True)
    # 关联用户
    user_id = Column(Integer, index=True, nullable=True, comment="用户 ID")
    # 交易时间
    ts = Column(DateTime, index=True, nullable=False, default=datetime.utcnow, comment="交易时间")
    # 金额（保存为正数）
    amount = Column(Float, nullable=False, comment="金额")
    # 类型：'income' (收入) / 'expense' (支出)
    type = Column(String(16), index=True, nullable=False, comment="收支类型")
    # 分类名称
    category = Column(String(64), index=True, default="其他", comment="分类")
    # 备注说明
    note = Column(Text, default="", comment="备注")
    # 记录创建时间（系统时间）
    created_at = Column(DateTime, default=datetime.utcnow, comment="创建时间")

    def __repr__(self) -> str:
        return f"<Record id={self.id} ts={self.ts} amount={self.amount} type={self.type} cat={self.category}>"


# 复合索引：优化“按类型+时间”的查询性能（如统计月度支出时）
Index("idx_records_type_ts", Record.type, Record.ts)


class User(Base):
    """用户表。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    uid = Column(String(64), unique=True, nullable=False, index=True, comment="登录 UID")
    username = Column(String(128), nullable=False, comment="自定义用户名")
    password_hash = Column(String(256), nullable=False, comment="密码哈希")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, comment="注册时间")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<User id={self.id} uid={self.uid}>"
