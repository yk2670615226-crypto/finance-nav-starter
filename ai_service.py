import pandas as pd
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.naive_bayes import MultinomialNB
from sqlalchemy.orm import Session

from models import Record


class CategoryPredictor:
    """
    记账分类预测器：
    - 优先使用基于历史记录训练的朴素贝叶斯模型
    - 当样本不足或模型不可用时，退回到简单关键字规则
    """

    def __init__(self) -> None:
        # 朴素贝叶斯文本分类器
        self.vectorizer = CountVectorizer(token_pattern=r"(?u)\b\w+\b")
        self.clf = MultinomialNB()
        self.is_trained: bool = False

        # 关键字规则（兜底逻辑）
        self.rules: dict[str, str] = {
            "饭": "餐饮",
            "餐": "餐饮",
            "吃": "餐饮",
            "KFC": "餐饮",
            "车": "交通",
            "油": "交通",
            "铁": "交通",
            "衣": "购物",
            "裤": "购物",
            "鞋": "购物",
            "买": "购物",
            "房": "居住",
            "电": "居住",
            "网": "居住",
            "药": "医疗",
            "医": "医疗",
            "玩": "娱乐",
            "游": "娱乐",
        }

    def train(self, db_session: Session, _user_id=None) -> None:
        """
        使用历史支出记录训练分类模型。

        说明：
        - 单用户模式，不使用 user_id，仅保留参数以兼容现有调用。
        - 只使用有备注的支出记录（type='expense' 且 note 非空）。
        - 样本少于 3 条时不训练模型，仅使用规则兜底。
        """
        records = (
            db_session.query(Record.note, Record.category)
            .filter(
                Record.note != "",
                Record.note.isnot(None),
                Record.type == "expense",
            )
            .all()
        )

        if len(records) < 3:
            self.is_trained = False
            return

        df = pd.DataFrame(records, columns=["note", "category"])
        try:
            X = self.vectorizer.fit_transform(df["note"])
            y = df["category"]
            self.clf.fit(X, y)
            self.is_trained = True
        except Exception:
            # 出现异常时关闭模型使用，退回规则模式
            self.is_trained = False

    def predict(self, note: str) -> str:
        """
        根据备注预测分类：
        1. 如果模型已训练且预测成功，返回模型结果；
        2. 否则按关键字规则匹配；
        3. 都失败则返回“其他”。
        """
        if not note:
            return "其他"

        text = str(note)

        if self.is_trained:
            try:
                X = self.vectorizer.transform([text])
                return self.clf.predict(X)[0]
            except Exception:
                # 模型预测失败时，继续走规则匹配
                pass

        for key, cat in self.rules.items():
            if key in text:
                return cat

        return "其他"


# 全局单例，供其他模块直接使用
predictor = CategoryPredictor()
