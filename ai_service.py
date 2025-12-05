import threading
from typing import Optional

import pandas as pd
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.naive_bayes import MultinomialNB
from sqlalchemy.orm import Session

from models import Record


class CategoryPredictor:
    """
    智能分类预测器。
    
    机制：
    1. 优先使用朴素贝叶斯模型根据历史备注进行预测。
    2. 如果模型未就绪或预测失败，回退到关键字规则匹配。
    """

    def __init__(self) -> None:
        # 简单的分词模式，支持中文
        self.vectorizer = CountVectorizer(token_pattern=r"(?u)\b\w+\b")
        self.clf = MultinomialNB()
        self.is_trained: bool = False
        
        # 线程锁，确保训练过程不会被并发打断
        self._train_lock = threading.Lock()

        # 兜底规则表
        self.rules: dict[str, str] = {
            "饭": "餐饮", "餐": "餐饮", "吃": "餐饮", "KFC": "餐饮", "麦当劳": "餐饮", "饿了么": "餐饮", "美团": "餐饮",
            "车": "交通", "油": "交通", "铁": "交通", "票": "交通", "滴滴": "交通",
            "衣": "购物", "裤": "购物", "鞋": "购物", "买": "购物", "淘": "购物", "京东": "购物",
            "房": "居住", "电": "居住", "网": "居住", "气": "居住", "物业": "居住",
            "药": "医疗", "医": "医疗", "体检": "医疗", "挂号": "医疗",
            "玩": "娱乐", "游": "娱乐", "影": "娱乐", "剧": "娱乐", "会员": "娱乐",
        }

    def train(self, db_session: Session, _user_id: Optional[int] = None) -> None:
        """
        从数据库加载数据并训练模型。
        建议在后台线程中调用。
        """
        # 如果当前正在训练，直接跳过本次请求，避免资源竞争
        if self._train_lock.locked():
            return

        with self._train_lock:
            # 仅使用有备注且为支出的记录进行训练
            records = (
                db_session.query(Record.note, Record.category)
                .filter(
                    Record.note != "",
                    Record.note.isnot(None),
                    Record.type == "expense",
                )
                .all()
            )

            # 样本太少时不训练
            if len(records) < 3:
                self.is_trained = False
                return

            try:
                df = pd.DataFrame(records, columns=["note", "category"])
                features = self.vectorizer.fit_transform(df["note"])
                labels = df["category"]
                self.clf.fit(features, labels)
                self.is_trained = True
            except Exception:
                # 训练出错（如数据格式问题）则回退状态
                self.is_trained = False

    def predict(self, note: str) -> str:
        """
        根据备注预测分类。
        """
        if not note:
            return "其他"

        text = str(note)

        # 1. 尝试模型预测
        if self.is_trained:
            try:
                X = self.vectorizer.transform([text])
                return self.clf.predict(X)[0]
            except Exception:
                pass

        # 2. 尝试规则匹配
        for key, cat in self.rules.items():
            if key in text:
                return cat

        # 3. 默认
        return "其他"


# 全局单例
predictor = CategoryPredictor()