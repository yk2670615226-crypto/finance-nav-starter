"""主要路由与业务逻辑，实现账本查询、导入导出等功能。"""

import calendar
import math
import os
import platform
import random
import re
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple

import pandas as pd
from flask import (
    Blueprint,
    Flask,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False
    _REPORTLAB_ERROR = "未安装 reportlab，无法生成 PDF 报表，请运行 'pip install -r requirements.txt' 后重试。"
from sqlalchemy import extract, func

from ai_service import predictor
from models import AppSettings, Record, User
from webapp import socketio
from webapp.config import get_data_path
from webapp.db import db_write_lock, get_db_session

bp = Blueprint("main", __name__)

MAX_IMPORT_SIZE_BYTES = 5 * 1024 * 1024
MAX_EXPORT_ROWS = 50000
MIN_REPORT_MONTHS = 1
MAX_REPORT_MONTHS = 36
TRAIN_DEBOUNCE_SECONDS = 30

AUTH_FREE_ENDPOINTS = {
    "main.login",
    "main.register",
    "main.do_login",
    "main.do_register",
    "static",
}

_train_timer: threading.Timer | None = None
_train_lock = threading.Lock()


def get_month_range(target_date: datetime) -> Tuple[datetime, datetime]:
    """计算目标日期所在月份的起止时间。"""
    start = target_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    _, last_day = calendar.monthrange(start.year, start.month)
    end = start.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def train_model_async(app: Flask) -> None:
    """异步触发智能分类模型训练，避免阻塞请求线程。"""

    def _train_task():
        with app.app_context():
            with get_db_session(app) as db_session:
                try:
                    predictor.train(db_session)
                except Exception as exc:
                    print(f"后台训练失败: {exc}")

    threading.Thread(target=_train_task, daemon=True).start()


def schedule_model_training() -> None:
    """通过防抖方式安排模型训练，减少频繁编辑导致的重复开销。"""

    app = current_app._get_current_object()

    def _schedule():
        train_model_async(app)

    global _train_timer
    with _train_lock:
        if _train_timer and _train_timer.is_alive():
            _train_timer.cancel()
        _train_timer = threading.Timer(TRAIN_DEBOUNCE_SECONDS, _schedule)
        _train_timer.daemon = True
        _train_timer.start()


def generate_demo_data(
    session_obj, user_id: int, target_records: int = 10000, years: int = 10
) -> None:
    """封装写锁，生成演示用的随机账单数据。"""
    with db_write_lock:
        _generate_demo_data(session_obj, user_id, target_records, years)


def _generate_demo_data(
    session_obj, user_id: int, target_records: int = 10000, years: int = 10
) -> None:
    """生成指定年份范围的随机收入支出记录。"""
    categories = {
        "餐饮": ["工作餐", "晚饭", "KFC", "火锅", "买菜", "奶茶", "夜宵", "烧烤"],
        "交通": ["地铁", "打车", "加油", "公交", "停车费", "高铁", "机票"],
        "购物": ["衣服", "日用品", "京东", "买鞋", "护肤品", "淘宝", "数码"],
        "娱乐": ["电影", "Steam", "KTV", "会员", "门票", "剧本杀", "旅游"],
        "居住": ["电费", "水费", "宽带", "燃气", "物业", "房租"],
        "医疗": ["买药", "挂号", "体检", "口罩"],
        "人情": ["红包", "请客", "礼物", "份子钱"],
    }
    income_notes = ["工资", "年终奖", "兼职", "理财收益", "闲鱼", "报销"]

    records = []
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365 * years)
    total_days = (end_date - start_date).days or 1

    month_factors = {}

    for _ in range(target_records):
        day_offset = random.randint(0, total_days)
        current_day = start_date + timedelta(days=day_offset)
        ts = current_day.replace(
            hour=random.randint(8, 22),
            minute=random.randint(0, 59),
            second=random.randint(0, 59),
        )

        m_key = (current_day.year, current_day.month)
        if m_key not in month_factors:
            month_factors[m_key] = random.uniform(0.8, 1.3)
        m_factor = month_factors[m_key]

        if random.random() < 0.12:
            rtype = "income"
            category = "收入"
            if current_day.day in (10, 15, 25) and random.random() < 0.6:
                note = "工资"
                amount = round(random.uniform(8000, 15000), 2)
            else:
                note = random.choice(income_notes)
                amount = round(random.uniform(100, 2000), 2)
        else:
            rtype = "expense"
            cat_key = random.choice(list(categories.keys()))
            category = cat_key
            note = random.choice(categories[cat_key])

            base_amt = random.uniform(10, 300)
            if cat_key in ["购物", "教育", "人情"]:
                base_amt = random.uniform(50, 1000)

            day_factor = 1.2 if current_day.weekday() >= 5 else 1.0
            amount = round(base_amt * day_factor * m_factor, 2)

        records.append(
            Record(
                ts=ts,
                amount=amount,
                type=rtype,
                category=category,
                note=note,
                user_id=user_id,
                is_demo=True,
            )
        )

    session_obj.add_all(records)

    expense_sum = sum(r.amount for r in records if r.type == "expense")
    months_count = years * 12
    avg_monthly = expense_sum / months_count if months_count else 5000
    base_budget = round(avg_monthly * 1.15 / 100) * 100

    cfg = session_obj.query(AppSettings).filter(AppSettings.user_id == user_id).first()
    if not cfg:
        cfg = AppSettings(user_id=user_id)
        session_obj.add(cfg)
    cfg.monthly_budget = base_budget
    cfg.enable_lock = False
    cfg.is_demo = True
    session_obj.commit()

    schedule_model_training()


def get_settings(db_session, user_id: int | None):
    """读取或初始化指定用户的全局配置。"""
    if not user_id:
        return AppSettings(monthly_budget=0.0, enable_lock=False, is_demo=False)

    cfg = db_session.query(AppSettings).filter(AppSettings.user_id == user_id).first()
    if not cfg:
        generate_demo_data(db_session, user_id)
        cfg = (
            db_session.query(AppSettings).filter(AppSettings.user_id == user_id).first()
        )
    return cfg


def get_budget_status(total: float, budget: float) -> Tuple[str, str]:
    """根据预算使用比例返回状态文本与样式。"""
    st_text = "预算正常"
    st_cls = "badge bg-success-subtle text-success border-success-subtle"

    if budget > 0:
        pct = (total / budget) * 100
        if pct >= 100:
            st_text, st_cls = (
                "已超支！",
                "badge bg-danger-subtle text-danger border-danger-subtle",
            )
        elif pct >= 80:
            st_text, st_cls = (
                "预警",
                "badge bg-warning-subtle text-warning border-warning-subtle",
            )
    else:
        st_text, st_cls = (
            "未设预算",
            "badge bg-light text-primary border-primary border-dashed",
        )

    return st_text, st_cls


def count_months(start: datetime, end: datetime) -> int:
    """计算两个日期间（含端点）的月份数量。"""
    return (end.year - start.year) * 12 + end.month - start.month + 1


def shift_year(dt: datetime, years: int = -1) -> datetime:
    """对日期进行年份偏移，并兼顾闰年日期安全。"""
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:
        safe_day = min(dt.day, 28)
        return dt.replace(year=dt.year + years, day=safe_day)


def summarize_monthly(records: list[Record]):
    """汇总记录的月度收入、支出与结余数据。"""
    monthly_map: dict[str, dict[str, float]] = {}
    for r in records:
        key = r.ts.strftime("%Y-%m")
        monthly_map.setdefault(key, {"income": 0.0, "expense": 0.0})
        monthly_map[key]["income" if r.type == "income" else "expense"] += r.amount

    monthly_rows = []
    for key in sorted(monthly_map.keys()):
        data = monthly_map[key]
        monthly_rows.append(
            {
                "月份": key,
                "收入": round(data.get("income", 0.0), 2),
                "支出": round(data.get("expense", 0.0), 2),
                "结余": round(data.get("income", 0.0) - data.get("expense", 0.0), 2),
            }
        )
    return monthly_rows


def summarize_totals(records: list[Record]) -> dict[str, float]:
    """统计记录集合的总收入、总支出与结余。"""
    income = sum(r.amount for r in records if r.type == "income")
    expense = sum(r.amount for r in records if r.type == "expense")
    return {
        "income": round(income, 2),
        "expense": round(expense, 2),
        "balance": round(income - expense, 2),
    }


@bp.before_app_request
def load_current_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        with get_db_session() as db_session:
            g.user = db_session.get(User, user_id)
            if not g.user:
                session.clear()

    g.user_id = g.user.id if g.user else None

    if request.endpoint in AUTH_FREE_ENDPOINTS or request.endpoint is None:
        return None

    if not g.user_id:
        next_url = request.path if request.method == "GET" else url_for("main.index")
        return redirect(url_for("main.login", next=next_url))


@bp.app_context_processor
def inject_config():
    with get_db_session() as db_session:
        return {"config": get_settings(db_session, g.get("user_id"))}


@bp.before_app_request
def check_app_lock():
    if request.endpoint in AUTH_FREE_ENDPOINTS or request.endpoint in [
        "main.lock_screen",
        "main.do_unlock",
    ]:
        return None

    if not g.get("user_id"):
        return None

    with get_db_session() as db_session:
        cfg = get_settings(db_session, g.user_id)
        if cfg.enable_lock and session.get("unlocked_user_id") != g.user_id:
            return redirect(url_for("main.lock_screen"))
    return None


@bp.teardown_app_request
def shutdown_session(exception=None):
    session_factory = current_app.config.get("SessionFactory")
    if session_factory:
        session_factory.remove()


@bp.route("/login")
def login():
    if g.get("user_id"):
        return redirect(url_for("main.index"))
    return render_template("login.html")


@bp.route("/register")
def register():
    if g.get("user_id"):
        return redirect(url_for("main.index"))
    return render_template("register.html")


@bp.post("/login")
def do_login():
    uid = (request.form.get("uid") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not uid or not password:
        flash("请输入 UID 和密码", "warning")
        return redirect(url_for("main.login"))

    with get_db_session() as db_session:
        user = db_session.query(User).filter(User.uid == uid).first()
        if not user or not user.check_password(password):
            flash("账号或密码错误", "danger")
            return redirect(url_for("main.login"))

    session["user_id"] = user.id
    session["username"] = user.username
    next_url = request.args.get("next") or url_for("main.index")
    return redirect(next_url)


@bp.post("/register")
def do_register():
    uid = (request.form.get("uid") or "").strip()
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not uid or not username or not password:
        flash("请完整填写所有字段", "warning")
        return redirect(url_for("main.register"))

    if not re.fullmatch(r"[A-Za-z0-9]+", uid):
        flash("UID 只能包含字母和数字", "danger")
        return redirect(url_for("main.register"))

    with get_db_session() as db_session:
        with db_write_lock:
            exists = db_session.query(User).filter(User.uid == uid).first()
            if exists:
                flash("UID 已被占用，请更换", "danger")
                return redirect(url_for("main.register"))

            user = User(uid=uid, username=username)
            user.set_password(password)
            db_session.add(user)
            db_session.commit()
            get_settings(db_session, user.id)

            session["user_id"] = user.id
            session["username"] = user.username

    return redirect(url_for("main.index"))


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


@bp.post("/profile/update_username")
def update_username():
    new_username = (request.form.get("username") or "").strip()
    if not new_username:
        flash("用户名不能为空", "warning")
        return redirect(request.referrer or url_for("main.index"))

    with get_db_session() as db_session:
        with db_write_lock:
            user = db_session.get(User, g.user_id)
            if not user:
                flash("用户不存在", "danger")
                return redirect(url_for("main.login"))

            user.username = new_username
            db_session.commit()
            session["username"] = new_username

    flash("用户名已更新", "success")
    return redirect(request.referrer or url_for("main.index"))


@bp.post("/profile/update_password")
def update_password():
    current_password = (request.form.get("current_password") or "").strip()
    new_password = (request.form.get("new_password") or "").strip()

    if not current_password or not new_password:
        flash("请输入当前密码和新密码", "warning")
        return redirect(request.referrer or url_for("main.index"))

    with get_db_session() as db_session:
        with db_write_lock:
            user = db_session.get(User, g.user_id)
            if not user:
                flash("用户不存在", "danger")
                return redirect(url_for("main.login"))

            if not user.check_password(current_password):
                flash("当前密码不正确", "danger")
                return redirect(request.referrer or url_for("main.index"))

            user.set_password(new_password)
            db_session.commit()

    flash("密码已更新", "success")
    return redirect(request.referrer or url_for("main.index"))


@bp.route("/lock")
def lock_screen():
    return render_template("lock.html")


@bp.route("/do_unlock", methods=["POST"])
def do_unlock():
    password = (request.form.get("password") or "").strip()
    if not password:
        flash("请输入密码", "warning")
        return redirect(url_for("main.lock_screen"))

    with get_db_session() as db_session:
        cfg = get_settings(db_session, g.user_id)
        if cfg.check_lock_password(password):
            session["unlocked_user_id"] = g.user_id
            return redirect(url_for("main.index"))

    flash("密码错误", "danger")
    return redirect(url_for("main.lock_screen"))


@bp.post("/lock/update")
def lock_update():
    data = request.form or (request.json or {})
    action = data.get("action", "").strip()

    with get_db_session() as db_session:
        cfg = get_settings(db_session, g.user_id)

        with db_write_lock:
            if action == "lock_enable":
                pwd = data.get("password", "").strip()
                if pwd:
                    cfg.set_lock_password(pwd)
                    cfg.enable_lock = True
                    db_session.commit()
                    flash("已开启应用锁", "success")
                else:
                    flash("密码不能为空", "danger")
            elif action == "lock_disable":
                cfg.enable_lock = False
                db_session.commit()
                session.pop("unlocked_user_id", None)
                flash("已关闭应用锁", "success")
    return redirect(request.referrer or url_for("main.index"))


@bp.route("/")
def index():
    now = datetime.now()
    start, end = get_month_range(now)

    with get_db_session() as db_session:
        cfg = get_settings(db_session, g.user_id)
        total_expense = (
            db_session.query(func.sum(Record.amount))
            .filter(
                Record.user_id == g.user_id,
                Record.type == "expense",
                Record.ts >= start,
                Record.ts <= end,
            )
            .scalar()
            or 0.0
        )

        cats = [
            r[0]
            for r in db_session.query(Record.category)
            .filter(Record.user_id == g.user_id, Record.category.isnot(None))
            .distinct()
        ]
        recent = (
            db_session.query(Record)
            .filter(Record.user_id == g.user_id)
            .order_by(Record.ts.desc())
            .limit(5)
            .all()
        )

    return render_template(
        "index.html",
        monthly_budget=cfg.monthly_budget,
        is_demo=cfg.is_demo,
        record_saved=request.args.get("saved") == "1",
        no_data=(cfg.monthly_budget == 0 and total_expense == 0),
        existing_categories=cats,
        recent_records=recent,
    )


@bp.route("/start_demo")
def start_demo():
    with get_db_session() as db_session:
        generate_demo_data(db_session, g.user_id)
    return redirect(url_for("main.index"))


@bp.route("/exit_demo", methods=["POST"])
def exit_demo():
    with get_db_session() as db_session:
        cfg = get_settings(db_session, g.user_id)
        if cfg.is_demo:
            with db_write_lock:
                (
                    db_session.query(Record)
                    .filter(Record.user_id == g.user_id, Record.is_demo.is_(True))
                    .delete(synchronize_session=False)
                )
                cfg.is_demo = False
                cfg.monthly_budget = 0
                db_session.commit()
            schedule_model_training()
            socketio.emit("update_pie")
    return redirect(url_for("main.index"))


@bp.route("/records")
def records_page():
    start = request.args.get("start_date")
    end = request.args.get("end_date")
    rtype = request.args.get("type", "").strip()
    cat = request.args.get("category", "").strip()
    sort = request.args.get("sort", "time_desc")
    page = request.args.get("page", 1, type=int)

    with get_db_session() as db_session:
        q = db_session.query(Record).filter(Record.user_id == g.user_id)

        if start:
            try:
                q = q.filter(Record.ts >= datetime.strptime(start, "%Y-%m-%d"))
            except ValueError:
                flash("开始日期格式不正确", "warning")
        if end:
            try:
                q = q.filter(
                    Record.ts
                    <= datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59)
                )
            except ValueError:
                flash("结束日期格式不正确", "warning")
        if rtype in ("income", "expense"):
            q = q.filter(Record.type == rtype)
        if cat:
            q = q.filter(Record.category == cat)

        sort_map = {
            "time_asc": Record.ts.asc(),
            "time_desc": Record.ts.desc(),
            "amount_asc": Record.amount.asc(),
            "amount_desc": Record.amount.desc(),
        }
        q = q.order_by(sort_map.get(sort, Record.ts.desc()))

        per_page = 20
        total = q.count()
        pages = math.ceil(total / per_page) if total else 1
        page = max(1, min(page, pages))

        records = q.offset((page - 1) * per_page).limit(per_page).all() if total else []
        cats = [
            r[0]
            for r in db_session.query(Record.category)
            .filter(Record.user_id == g.user_id)
            .distinct()
        ]

    q_params = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template(
        "records.html",
        records=records,
        existing_categories=cats,
        current_page=page,
        total_pages=pages,
        total_count=total,
        query_params=q_params,
    )


@bp.route("/report/annual")
def annual_report():
    year = request.args.get("year", datetime.now().year, type=int)
    start = datetime(year, 1, 1)
    end = datetime(year, 12, 31, 23, 59, 59)

    with get_db_session() as db_session:
        totals = (
            db_session.query(Record.type, func.sum(Record.amount))
            .filter(Record.user_id == g.user_id, Record.ts >= start, Record.ts <= end)
            .group_by(Record.type)
            .all()
        )
        sums = {t: (v or 0) for t, v in totals}

        total_exp = sums.get("expense", 0)
        total_inc = sums.get("income", 0)

        max_rec = (
            db_session.query(Record)
            .filter(
                Record.user_id == g.user_id,
                Record.type == "expense",
                Record.ts >= start,
                Record.ts <= end,
            )
            .order_by(Record.amount.desc())
            .first()
        )

        cat_stats = (
            db_session.query(Record.category, func.sum(Record.amount))
            .filter(
                Record.user_id == g.user_id,
                Record.type == "expense",
                Record.ts >= start,
                Record.ts <= end,
            )
            .group_by(Record.category)
            .order_by(func.sum(Record.amount).desc())
            .all()
        )

    top_4 = [{"name": c, "value": v} for c, v in cat_stats[:4]]
    while len(top_4) < 4:
        top_4.append({"name": "---", "value": 0})

    years_list = []
    amounts_list = []
    with get_db_session() as db_session:
        for y in range(year - 4, year + 1):
            y_s = datetime(y, 1, 1)
            y_e = datetime(y, 12, 31, 23, 59, 59)
            val = (
                db_session.query(func.sum(Record.amount))
                .filter(
                    Record.user_id == g.user_id,
                    Record.type == "expense",
                    Record.ts >= y_s,
                    Record.ts <= y_e,
                )
                .scalar()
                or 0
            )
            years_list.append(str(y))
            amounts_list.append(round(val, 2))

    pie_data = [{"name": c, "value": round(v, 2)} for c, v in cat_stats[:5]]
    other = total_exp - sum(x["value"] for x in pie_data)
    if other > 0:
        pie_data.append({"name": "其他", "value": round(other, 2)})

    return render_template(
        "annual_report.html",
        year=year,
        total_expense=total_exp,
        total_income=total_inc,
        balance=total_inc - total_exp,
        max_expense_val=max_rec.amount if max_rec else 0,
        max_expense_note=max_rec.note if max_rec else "无",
        top_4_stats=top_4,
        years_list=years_list,
        amounts_list=amounts_list,
        pie_data=pie_data,
    )


@bp.post("/records/new")
def records_new():
    rtype = request.form.get("type")
    note = (request.form.get("note", "") or "").strip()
    manual_cat = (request.form.get("category", "") or "").strip()

    try:
        amount = float(request.form.get("amount", 0))
    except (TypeError, ValueError):
        flash("金额格式不正确", "danger")
        return redirect(url_for("main.index"))

    if amount <= 0:
        flash("金额必须大于 0", "danger")
        return redirect(url_for("main.index"))

    if rtype not in ("income", "expense"):
        flash("收支类型无效", "danger")
        return redirect(url_for("main.index"))

    cat = (
        "收入"
        if rtype == "income"
        else (manual_cat if manual_cat else predictor.predict(note))
    )

    with get_db_session() as db_session:
        with db_write_lock:
            db_session.add(
                Record(
                    ts=datetime.now(),
                    amount=amount,
                    type=rtype,
                    category=cat,
                    note=note,
                    user_id=g.user_id,
                    is_demo=False,
                )
            )
            db_session.commit()

    if rtype == "expense":
        schedule_model_training()

    socketio.emit("update_pie")
    return redirect(url_for("main.index", saved="1"))


@bp.post("/records/update")
def records_update():
    try:
        rec_id = int(request.form.get("id", 0))
    except (TypeError, ValueError):
        flash("记录 ID 无效", "danger")
        return redirect(url_for("main.records_page"))

    with get_db_session() as db_session:
        rec = (
            db_session.query(Record)
            .filter(Record.id == rec_id, Record.user_id == g.user_id)
            .first()
        )
        if not rec:
            flash("记录不存在", "warning")
            return redirect(url_for("main.records_page"))

        with db_write_lock:
            try:
                amount_val = float(request.form.get("amount", rec.amount))
                if amount_val <= 0:
                    raise ValueError
                rec.amount = amount_val
            except (TypeError, ValueError):
                flash("金额格式不正确", "danger")
                return redirect(url_for("main.records_page"))

            rec.category = (request.form.get("category", "") or "").strip()
            rec.note = (request.form.get("note", "") or "").strip()
            db_session.commit()

        if rec.type == "expense":
            schedule_model_training()
        socketio.emit("update_pie")
    return redirect(url_for("main.records_page"))


@bp.post("/records/delete")
def records_delete():
    ids = [int(x) for x in request.form.getlist("selected_ids") if x.isdigit()]
    if not ids:
        flash("请选择要删除的记录", "warning")
        return redirect(url_for("main.records_page"))

    with get_db_session() as db_session:
        with db_write_lock:
            db_session.query(Record).filter(
                Record.user_id == g.user_id, Record.id.in_(ids)
            ).delete(synchronize_session=False)
            db_session.commit()

    schedule_model_training()
    socketio.emit("update_pie")
    return redirect(url_for("main.records_page"))


@bp.post("/api/predict")
def api_predict():
    data = request.json or {}
    return jsonify({"category": predictor.predict(data.get("note", ""))})


@bp.post("/api/update_budget")
def api_update_budget():
    try:
        val = float((request.json or {}).get("budget", 0))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "预算格式不正确"})

    if val < 0:
        return jsonify({"success": False, "error": "预算不能为负数"})

    with get_db_session() as db_session:
        with db_write_lock:
            cfg = get_settings(db_session, g.user_id)
            cfg.monthly_budget = val
            db_session.commit()

    socketio.emit("update_pie")
    return jsonify({"success": True})


@bp.route("/api/stats_trend")
def api_stats_trend():
    mode = request.args.get("mode", "month")
    try:
        target = datetime.strptime(request.args.get("date", ""), "%Y-%m")
    except ValueError:
        target = datetime.now()

    with get_db_session() as db_session:
        if mode == "year":
            start = target.replace(month=1, day=1, hour=0, minute=0, second=0)
            end = target.replace(month=12, day=31, hour=23, minute=59, second=59)
            q = (
                db_session.query(
                    extract("month", Record.ts).label("k"), func.sum(Record.amount)
                )
                .filter(
                    Record.user_id == g.user_id,
                    Record.type == "expense",
                    Record.ts >= start,
                    Record.ts <= end,
                )
                .group_by("k")
                .all()
            )
            data = {k: v for k, v in q}
            labels = [f"{m}月" for m in range(1, 13)]
            values = [round(data.get(m, 0) or 0, 2) for m in range(1, 13)]
            title = f"{target.year}年 支出趋势"
        else:
            start, end = get_month_range(target)
            _, days = calendar.monthrange(target.year, target.month)
            q = (
                db_session.query(
                    extract("day", Record.ts).label("k"), func.sum(Record.amount)
                )
                .filter(
                    Record.user_id == g.user_id,
                    Record.type == "expense",
                    Record.ts >= start,
                    Record.ts <= end,
                )
                .group_by("k")
                .all()
            )
            data = {k: v for k, v in q}
            labels = [f"{d}日" for d in range(1, days + 1)]
            values = [round(data.get(d, 0) or 0, 2) for d in range(1, days + 1)]
            title = f"{target.year}年{target.month}月 支出趋势"

    return jsonify({"labels": labels, "values": values, "title": title})


@bp.route("/api/stats_category")
def api_stats_category():
    try:
        target = datetime.strptime(request.args.get("date", ""), "%Y-%m")
    except ValueError:
        target = datetime.now()
    start, end = get_month_range(target)

    with get_db_session() as db_session:
        raw_data = (
            db_session.query(Record.category, func.sum(Record.amount))
            .filter(
                Record.user_id == g.user_id,
                Record.type == "expense",
                Record.ts >= start,
                Record.ts <= end,
            )
            .group_by(Record.category)
            .all()
        )

    pie_data = [
        {"name": cat, "value": round(amount or 0.0, 2)} for cat, amount in raw_data
    ]

    return jsonify(pie_data)


@bp.route("/api/summary")
def api_summary():
    try:
        target = datetime.strptime(request.args.get("date", ""), "%Y-%m")
    except ValueError:
        target = datetime.now()

    start, end = get_month_range(target)

    with get_db_session() as db_session:
        total = (
            db_session.query(func.sum(Record.amount))
            .filter(
                Record.user_id == g.user_id,
                Record.type == "expense",
                Record.ts >= start,
                Record.ts <= end,
            )
            .scalar()
            or 0.0
        )
        cfg = get_settings(db_session, g.user_id)
        bg = cfg.monthly_budget

    st_text, st_cls = get_budget_status(total, bg)

    return jsonify(
        {
            "display_spent": f"{total:.2f}",
            "display_budget": f"{bg:.2f}" if bg > 0 else "/",
            "status_text": st_text,
            "status_class": st_cls,
            "date_title": f"{target.year}年{target.month}月",
        }
    )


@bp.post("/api/import_data")
def api_import_data():
    if request.content_length and request.content_length > MAX_IMPORT_SIZE_BYTES:
        return jsonify(
            {"success": False, "msg": "文件过大，请压缩或分批导入（上限 5MB）"}
        )

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"success": False, "msg": "文件无效"})

    filename = f.filename.lower()
    if not (
        filename.endswith(".csv")
        or filename.endswith(".xlsx")
        or filename.endswith(".xls")
    ):
        return jsonify({"success": False, "msg": "仅支持 CSV 或 Excel 文件"})

    try:
        if filename.endswith(".csv"):
            try:
                df = pd.read_csv(f)
            except Exception:
                f.seek(0)
                df = pd.read_csv(f, encoding="gbk")
        else:
            df = pd.read_excel(f)

        col_map = {
            "时间": "ts",
            "日期": "ts",
            "Date": "ts",
            "金额": "amount",
            "Price": "amount",
            "分类": "category",
            "类别": "category",
            "备注": "note",
            "说明": "note",
            "类型": "type",
            "收支": "type",
        }
        df.rename(columns=lambda x: col_map.get(str(x).strip(), x), inplace=True)

        if "amount" not in df.columns:
            return jsonify({"success": False, "msg": "缺少金额列"})

        df["ts"] = pd.to_datetime(df.get("ts", datetime.now()), errors="coerce").fillna(
            datetime.now()
        )
        df["category"] = df.get("category", "导入").fillna("其他").astype(str)
        df["note"] = df.get("note", "").fillna("").astype(str)

        recs = []
        for _, row in df.iterrows():
            try:
                amt = float(row["amount"])
            except Exception:
                continue

            tp = str(row.get("type", "")).strip()
            final_tp = "income" if tp in ["收入", "income", "Income"] else "expense"

            recs.append(
                Record(
                    ts=row["ts"],
                    amount=abs(amt),
                    type=final_tp,
                    category=row["category"],
                    note=row["note"],
                    user_id=g.user_id,
                    is_demo=False,
                )
            )

        if not recs:
            return jsonify({"success": False, "msg": "无有效数据"})

        with get_db_session() as db_session:
            with db_write_lock:
                db_session.add_all(recs)
                db_session.commit()

            schedule_model_training()
        socketio.emit("update_pie")
        return jsonify({"success": True, "count": len(recs)})

    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)})


def build_comparison_rows(current_label: str, prev_label: str, recs, prev_recs):
    """生成当前周期与对比周期的汇总行。"""
    current_total = summarize_totals(recs)
    prev_total = summarize_totals(prev_recs)
    diff_income = current_total["income"] - prev_total["income"]
    diff_expense = current_total["expense"] - prev_total["expense"]

    return [
        {
            "期间": current_label,
            "总收入": current_total["income"],
            "总支出": current_total["expense"],
            "结余": current_total["balance"],
            "同比收入差额": round(diff_income, 2),
            "同比支出差额": round(diff_expense, 2),
        },
        {
            "期间": prev_label,
            "总收入": prev_total["income"],
            "总支出": prev_total["expense"],
            "结余": prev_total["balance"],
            "同比收入差额": "基准",
            "同比支出差额": "基准",
        },
    ]


def create_excel_report(
    path: Path,
    recs: list[Record],
    monthly_rows: list[dict[str, float]],
    comparison_rows,
    include_comparison: bool,
):
    """生成包含流水、月度汇总和可选年度对比的 Excel 报表。"""
    record_data = [
        {
            "时间": r.ts,
            "类型": ("收入" if r.type == "income" else "支出"),
            "金额": r.amount,
            "类别": r.category,
            "备注": r.note,
        }
        for r in recs
    ]

    with pd.ExcelWriter(path) as writer:
        pd.DataFrame(record_data).to_excel(writer, index=False, sheet_name="流水明细")
        pd.DataFrame(monthly_rows).to_excel(writer, index=False, sheet_name="月度汇总")

        if include_comparison and comparison_rows:
            pd.DataFrame(comparison_rows).to_excel(
                writer, index=False, sheet_name="年度对比"
            )
        else:
            pd.DataFrame([{"说明": "选定范围小于 12 个月，未生成年度对比。"}]).to_excel(
                writer, index=False, sheet_name="年度对比"
            )


def create_pdf_report(
    path: Path,
    recs: list[Record],
    monthly_rows: list[dict[str, float]],
    comparison_rows,
    include_comparison: bool,
    start: datetime,
    end: datetime,
):
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    styles = getSampleStyleSheet()
    heading_style = styles["Heading2"].clone("ChineseHeading")
    heading_style.fontName = "STSong-Light"
    heading_style.leading = 18

    body_style = styles["BodyText"].clone("ChineseBody")
    body_style.fontName = "STSong-Light"
    body_style.fontSize = 11
    body_style.leading = 16

    title_style = styles["Heading1"].clone("ChineseTitle")
    title_style.fontName = "STSong-Light"
    title_style.spaceAfter = 12

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=48,
        bottomMargin=36,
    )

    elements = [
        Paragraph("财务报表", title_style),
        Paragraph(
            f"周期：{start.strftime('%Y-%m-%d')} 至 {end.strftime('%Y-%m-%d')}",
            body_style,
        ),
    ]

    totals = summarize_totals(recs)
    elements.append(
        Paragraph(
            f"总收入：{totals['income']:.2f}，总支出：{totals['expense']:.2f}，结余：{totals['balance']:.2f}",
            body_style,
        )
    )
    elements.append(Spacer(1, 12))

    def render_table(title: str, data: list[list[str | float]]):
        elements.append(Paragraph(title, heading_style))
        table = Table(data, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.darkblue),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                ]
            )
        )
        elements.append(table)
        elements.append(Spacer(1, 12))

    monthly_data = [["月份", "收入", "支出", "结余"]]
    for row in monthly_rows:
        monthly_data.append(
            [
                row["月份"],
                f"{row['收入']:.2f}",
                f"{row['支出']:.2f}",
                f"{row['结余']:.2f}",
            ]
        )
    render_table("月度汇总", monthly_data)

    if include_comparison:
        comparison_data = [
            ["期间", "总收入", "总支出", "结余", "同比收入差额", "同比支出差额"]
        ]
        for row in comparison_rows or []:
            comparison_data.append(
                [
                    row["期间"],
                    (
                        f"{row['总收入']:.2f}"
                        if isinstance(row["总收入"], (int, float))
                        else row["总收入"]
                    ),
                    (
                        f"{row['总支出']:.2f}"
                        if isinstance(row["总支出"], (int, float))
                        else row["总支出"]
                    ),
                    (
                        f"{row['结余']:.2f}"
                        if isinstance(row["结余"], (int, float))
                        else row["结余"]
                    ),
                    row["同比收入差额"],
                    row["同比支出差额"],
                ]
            )
        render_table("年度对比", comparison_data)

    top_records = sorted(recs, key=lambda x: x.ts, reverse=True)[:20]
    record_data = [["日期", "类型", "金额", "类别", "备注"]]
    for r in top_records:
        record_data.append(
            [
                r.ts.strftime("%Y-%m-%d"),
                "收入" if r.type == "income" else "支出",
                f"{r.amount:.2f}",
                r.category,
                r.note,
            ]
        )
    render_table("最近记录", record_data)

    doc.build(elements)


@bp.post("/api/export_report")
@bp.post("/api/export_excel")
def api_export_report():
    data = request.json or request.form or {}
    start_str = data.get("start_date")
    end_str = data.get("end_date")
    fmt = (data.get("format") or "excel").lower()

    if fmt not in ("excel", "pdf"):
        return jsonify({"success": False, "msg": "仅支持 PDF 或 Excel 导出"})

    if not start_str or not end_str:
        return jsonify({"success": False, "msg": "请选择报表的开始和结束日期"})

    if fmt == "pdf" and not HAS_REPORTLAB:
        return jsonify({"success": False, "msg": _REPORTLAB_ERROR})

    try:
        start = datetime.strptime(start_str, "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = datetime.strptime(end_str, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
    except ValueError:
        return jsonify({"success": False, "msg": "日期格式不正确"})

    if end < start:
        return jsonify({"success": False, "msg": "结束日期不能早于开始日期"})

    months_span = count_months(start, end)
    if months_span < MIN_REPORT_MONTHS or months_span > MAX_REPORT_MONTHS:
        return jsonify(
            {
                "success": False,
                "msg": "覆盖范围需在 1-36 个月内，请重新选择日期",
            }
        )

    with get_db_session() as db_session:
        recs = (
            db_session.query(Record)
            .filter(Record.user_id == g.user_id, Record.ts >= start, Record.ts <= end)
            .order_by(Record.ts.asc())
            .all()
        )

        if not recs:
            return jsonify({"success": False, "msg": "选定时间内无数据"})
        if len(recs) > MAX_EXPORT_ROWS:
            return jsonify({"success": False, "msg": "数据量过大，请缩小时间范围"})

        comparison_recs = []
        include_comparison = months_span >= 12
        if include_comparison:
            prev_start = shift_year(start)
            prev_end = shift_year(end)
            comparison_recs = (
                db_session.query(Record)
                .filter(
                    Record.user_id == g.user_id,
                    Record.ts >= prev_start,
                    Record.ts <= prev_end,
                )
                .order_by(Record.ts.asc())
                .all()
            )

    monthly_rows = summarize_monthly(recs)
    comparison_rows = []
    if include_comparison:
        comparison_rows = build_comparison_rows(
            f"当前区间（{start.strftime('%Y-%m-%d')} 至 {end.strftime('%Y-%m-%d')})",
            f"对比区间（{shift_year(start).strftime('%Y-%m-%d')} 至 {shift_year(end).strftime('%Y-%m-%d')})",
            recs,
            comparison_recs,
        )

    desktop = Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    filename = f"财务报表_{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}.{ 'xlsx' if fmt == 'excel' else 'pdf'}"
    path = desktop / filename

    try:
        if fmt == "excel":
            create_excel_report(
                path, recs, monthly_rows, comparison_rows, include_comparison
            )
        else:
            create_pdf_report(
                path,
                recs,
                monthly_rows,
                comparison_rows,
                include_comparison,
                start,
                end,
            )

        if platform.system() == "Windows":
            subprocess.run(["explorer", "/select,", str(path)], check=False)

        return jsonify({"success": True, "path": str(path), "format": fmt})
    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)})


@bp.route("/open_data_folder")
def open_data_folder():
    path = str(get_data_path())
    try:
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        flash(f"已打开: {path}", "success")
    except Exception:
        flash("打开失败", "danger")
    return redirect(url_for("main.index"))
