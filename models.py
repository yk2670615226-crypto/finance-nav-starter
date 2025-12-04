from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, Text, Boolean, DateTime, Index
from sqlalchemy.orm import declarative_base
from werkzeug.security import generate_password_hash, check_password_hash

Base = declarative_base()


class AppSettings(Base):
    """应用全局配置（单用户，一般仅一条记录）。"""

    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    # 月预算，单位与记账金额一致
    monthly_budget = Column(Float, default=0.0)

    # 应用锁相关
    enable_lock = Column(Boolean, default=False)
    lock_password_hash = Column(String(256), nullable=True)

    # 是否处于演示模式
    is_demo = Column(Boolean, default=False)

    def set_lock_password(self, password: str) -> None:
        """设置应用锁密码（只保存哈希值，不保存明文）。"""
        self.lock_password_hash = generate_password_hash(password)

    def check_lock_password(self, password: str) -> bool:
        """校验应用锁密码。"""
        if not self.lock_password_hash:
            return False
        return check_password_hash(self.lock_password_hash, password)


class Record(Base):
    """收支记录（单用户，无 user_id 字段）。"""

    __tablename__ = "records"

    id = Column(Integer, primary_key=True)
    # 记账时间
    ts = Column(DateTime, index=True, nullable=False, default=datetime.utcnow)
    # 金额，正数
    amount = Column(Float, nullable=False)
    # 类型：'income' / 'expense'
    type = Column(String(16), index=True, nullable=False)
    # 分类名称
    category = Column(String(64), index=True, default="其他")
    # 备注
    note = Column(Text, default="")
    # 记录创建时间
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        """调试用的简短字符串表示。"""
        return (
            f"<Record id={self.id} ts={self.ts!r} amount={self.amount} "
            f"type={self.type!r} category={self.category!r}>"
        )


# 组合索引：按类型+时间查询时更高效
Index("idx_records_type_ts", Record.type, Record.ts)
