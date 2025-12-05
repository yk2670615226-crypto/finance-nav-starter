import re
import threading
from typing import List, Optional

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
        self.vectorizer = CountVectorizer(tokenizer=self._tokenize, token_pattern=None)
        self.clf = MultinomialNB()
        self.is_trained: bool = False

        self._train_lock = threading.Lock()
        self._model_lock = threading.RLock()

        self.rules: dict[str, str] = {
            "饭": "餐饮",
            "餐": "餐饮",
            "吃": "餐饮",
            "KFC": "餐饮",
            "麦当劳": "餐饮",
            "饿了么": "餐饮",
            "美团": "餐饮",
            "车": "交通",
            "油": "交通",
            "铁": "交通",
            "票": "交通",
            "滴滴": "交通",
            "衣": "购物",
            "裤": "购物",
            "鞋": "购物",
            "买": "购物",
            "淘": "购物",
            "京东": "购物",
            "房": "居住",
            "电": "居住",
            "网": "居住",
            "气": "居住",
            "物业": "居住",
            "药": "医疗",
            "医": "医疗",
            "体检": "医疗",
            "挂号": "医疗",
            "玩": "娱乐",
            "游": "娱乐",
            "影": "娱乐",
            "剧": "娱乐",
            "会员": "娱乐",
        }

    def _tokenize(self, text: str) -> List[str]:
        normalized = str(text).strip()
        if not normalized:
            return []

        tokens = re.findall(r"[A-Za-z]+|\d+|[\u4e00-\u9fff]", normalized)
        bigrams = [a + b for a, b in zip(tokens, tokens[1:])]
        return tokens + bigrams

    def train(self, db_session: Session, _user_id: Optional[int] = None) -> None:
        if self._train_lock.locked():
            return

        with self._train_lock:
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

            try:
                df = pd.DataFrame(records, columns=["note", "category"])
                with self._model_lock:
                    new_vectorizer = CountVectorizer(tokenizer=self._tokenize, token_pattern=None)
                    new_clf = MultinomialNB()

                    features = new_vectorizer.fit_transform(df["note"])
                    labels = df["category"]
                    new_clf.fit(features, labels)

                    self.vectorizer = new_vectorizer
                    self.clf = new_clf
                    self.is_trained = True
            except Exception:
                self.is_trained = False

    def predict(self, note: str) -> str:
        if not note:
            return "其他"

        text = str(note)

        if self.is_trained:
            try:
                with self._model_lock:
                    X = self.vectorizer.transform([text])
                    return self.clf.predict(X)[0]
            except Exception:
                pass

        for key, cat in self.rules.items():
            if key in text:
                return cat

        return "其他"


predictor = CategoryPredictor()
