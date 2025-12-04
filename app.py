import os
import sys
import random
import calendar
import math
import platform
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    jsonify,
    flash,
    session,
)
from flask_socketio import SocketIO
from sqlalchemy import create_engine, func, extract
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import NullPool

from models import Base, Record, AppSettings
from ai_service import predictor


# ===== 工具函数 =====
def get_base_path():
    """获取资源文件路径 (HTML/CSS)"""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def get_data_path():
    """
    数据库存放位置：
    - 开发模式：项目目录
    - 打包 EXE：放到系统 AppData 下
    """
    if getattr(sys, "frozen", False):
        home = os.path.expanduser("~")
        if platform.system() == "Windows":
            base_data_dir = os.path.join(home, "AppData", "Roaming")
        elif platform.system() == "Darwin":
            base_data_dir = os.path.join(home, "Library", "Application Support")
        else:
            base_data_dir = os.path.join(home, ".local", "share")

        app_data_dir = os.path.join(base_data_dir, "MyAccounting")
        os.makedirs(app_data_dir, exist_ok=True)
        return app_data_dir
    else:
        return os.path.dirname(os.path.abspath(__file__))


def get_month_range(target_date: datetime):
    """给定某天，返回当月的起止时间区间"""
    start = target_date.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    _, last_day = calendar.monthrange(start.year, start.month)
    end = start.replace(
        day=last_day,
        hour=23,
        minute=59,
        second=59,
        microsecond=999999,
    )
    return start, end


# ===== 演示数据生成 =====
def generate_demo_data(session, target_records: int = 10000, years: int = 10):
    """
    生成演示数据：
    - 默认 10000 条记录
    - 时间范围覆盖最近 10 年
    - 自动根据生成的支出数据估算一个相对合理的月度预算
    """
    categories = {
        "餐饮": [
            "工作餐",
            "晚饭",
            "KFC",
            "麦当劳",
            "火锅",
            "买菜",
            "奶茶",
            "夜宵",
            "星巴克",
            "水果",
        ],
        "交通": [
            "地铁充值",
            "打车",
            "加油",
            "公交",
            "停车费",
            "高铁",
            "机票",
            "保养",
        ],
        "购物": [
            "衣服",
            "日用品",
            "京东",
            "买鞋",
            "护肤品",
            "淘宝",
            "便利店",
            "数码配件",
            "家具",
        ],
        "娱乐": [
            "电影",
            "Steam",
            "KTV",
            "会员",
            "门票",
            "剧本杀",
            "聚会",
            "旅行",
        ],
        "居住": ["电费", "水费", "宽带", "燃气", "物业", "鲜花", "房租"],
        "医疗": ["买药", "挂号", "体检", "口罩", "疫苗"],
        "教育": ["买书", "课程", "考试费"],
        "人情": ["红包", "请客", "礼物"],
    }
    income_notes = [
        "工资",
        "年终奖",
        "兼职",
        "理财收益",
        "闲鱼回血",
        "红包",
        "报销",
        "奖金",
    ]

    records = []

    end_date = datetime.now()
    start_date = end_date - timedelta(days=365 * years)
    total_days = (end_date - start_date).days or 1  # 防止除零

    # 为每个月生成一个稳定的消费波动系数，让不同月份支出水平有差异
    month_factor_cache: dict[tuple[int, int], float] = {}

    def get_month_factor(dt: datetime) -> float:
        key = (dt.year, dt.month)
        if key not in month_factor_cache:
            # 使用固定种子保证同一月份的因子稳定
            random.seed(dt.year * 100 + dt.month)
            month_factor_cache[key] = random.uniform(0.8, 1.3)
            random.seed()  # 重置全局随机种子
        return month_factor_cache[key]

    for _ in range(target_records):
        # 随机选取某一天
        day_offset = random.randint(0, total_days)
        current_day = start_date + timedelta(days=day_offset)

        # 随机一天中的时间
        ts = current_day.replace(
            hour=random.randint(8, 22),
            minute=random.randint(0, 59),
            second=random.randint(0, 59),
            microsecond=0,
        )

        # 约 12% 为收入记录，其余为支出
        if random.random() < 0.12:
            rtype = "income"
            category = "收入"

            # 大概率每月 10 / 15 / 25 号发工资
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

            # 基础金额（不同类别有不同区间）
            if cat_key == "餐饮":
                base_amt = random.uniform(20, 120)
            elif cat_key == "交通":
                base_amt = random.uniform(5, 80)
            elif cat_key == "购物":
                base_amt = random.uniform(30, 1500)
            elif cat_key == "居住":
                base_amt = random.uniform(80, 800)
            elif cat_key == "娱乐":
                base_amt = random.uniform(20, 600)
            elif cat_key == "医疗":
                base_amt = random.uniform(30, 500)
            elif cat_key == "教育":
                base_amt = random.uniform(50, 2000)
            elif cat_key == "人情":
                base_amt = random.uniform(50, 2000)
            else:
                base_amt = random.uniform(10, 400)

            # 周末略微放大消费
            day_factor = 1.2 if current_day.weekday() >= 5 else 1.0
            # 不同月份整体水平不同
            month_factor = get_month_factor(current_day)

            amount = round(base_amt * day_factor * month_factor, 2)

        records.append(
            Record(
                ts=ts,
                amount=amount,
                type=rtype,
                category=category,
                note=note,
            )
        )

    # 写入数据库
    session.add_all(records)

    # 计算各月总支出，用于估算一个“合理”的预算
    expense_by_month = defaultdict(float)
    for r in records:
        if r.type == "expense":
            expense_by_month[(r.ts.year, r.ts.month)] += r.amount

    if expense_by_month:
        monthly_totals = list(expense_by_month.values())
        avg_monthly = sum(monthly_totals) / len(monthly_totals)
        # 预算设为“平均月支出 * 1.15”，向百位取整
        base_budget = round(avg_monthly * 1.15 / 100) * 100
    else:
        base_budget = 8000.0

    # 配置项
    cfg = session.query(AppSettings).first()
    if not cfg:
        cfg = AppSettings()
        session.add(cfg)

    cfg.monthly_budget = base_budget
    cfg.enable_lock = False
    cfg.is_demo = True

    session.commit()

    # 用演示数据训练分类模型（失败就算了）
    try:
        predictor.train(session, None)
    except Exception:
        pass


# ===== 基础配置 =====
BASE_DIR = get_base_path()
DATA_DIR = get_data_path()
DB_PATH = os.path.join(DATA_DIR, "finance.db")

print(f"数据库存储路径: {DB_PATH}")

TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.config["SECRET_KEY"] = "my_local_secret_key"
socketio = SocketIO(app, cors_allowed_origins="*")

engine = create_engine(
    f"sqlite:///{DB_PATH}?check_same_thread=False",
    poolclass=NullPool,
    future=True,
)
Base.metadata.create_all(engine)
Session = scoped_session(sessionmaker(bind=engine))


# ===== 配置读取 =====
def get_settings(s):
    """获取或初始化全局配置（首次启动会自动生成演示数据）"""
    cfg = s.query(AppSettings).first()
    if not cfg:
        print("首次启动，初始化演示数据...")
        cfg = AppSettings(monthly_budget=8000.0, enable_lock=False, is_demo=True)
        s.add(cfg)
        generate_demo_data(s)
        cfg = s.query(AppSettings).first()
    return cfg


@app.context_processor
def inject_config():
    """让所有模板都能拿到 config（比如头部导航里的锁图标状态）"""
    s = Session()
    cfg = get_settings(s)
    return {"config": cfg}


# ===== 中间件：应用锁 =====
@app.before_request
def check_app_lock():
    """应用锁：除静态资源和锁相关路由外，其他访问需先解锁"""
    if request.endpoint in ["static", "lock_screen", "do_unlock"]:
        return

    s = Session()
    cfg = get_settings(s)
    if cfg.enable_lock and not session.get("is_unlocked"):
        return redirect(url_for("lock_screen"))


# ===== 锁屏相关 =====
@app.route("/lock")
def lock_screen():
    return render_template("lock.html")


@app.route("/do_unlock", methods=["POST"])
def do_unlock():
    password = request.form.get("password")
    s = Session()
    cfg = get_settings(s)
    if cfg.check_lock_password(password):
        session["is_unlocked"] = True
        return redirect(url_for("index"))
    else:
        flash("密码错误", "danger")
        return redirect(url_for("lock_screen"))


@app.post("/lock/update")
def lock_update():
    """
    从导航弹窗开启 / 关闭应用锁
    - action = lock_enable + password
    - action = lock_disable
    """
    s = Session()
    cfg = get_settings(s)

    data = request.form if request.form else (request.json or {})
    action = (data.get("action") or "").strip()

    if action == "lock_enable":
        password = (data.get("password") or "").strip()
        if not password:
            flash("密码不能为空", "danger")
        else:
            cfg.set_lock_password(password)
            cfg.enable_lock = True
            s.commit()
            flash("已开启应用锁", "success")

    elif action == "lock_disable":
        cfg.enable_lock = False
        s.commit()
        # 关闭应用锁后就不需要解锁状态了
        session.pop("is_unlocked", None)
        flash("已关闭应用锁", "success")

    return redirect(request.referrer or url_for("index"))


# ===== 页面：主页 / 记录 / 年度报表 =====
@app.route("/")
def index():
    s = Session()
    cfg = get_settings(s)
    now = datetime.now()
    start, end = get_month_range(now)

    total_expense = (
        s.query(func.sum(Record.amount))
        .filter(Record.type == "expense", Record.ts >= start, Record.ts < end)
        .scalar()
        or 0.0
    )

    no_data = (cfg.monthly_budget == 0) and (total_expense == 0)
    cats = [
        r[0]
        for r in s.query(Record.category)
        .filter(Record.category != None)  # noqa: E711
        .distinct()
        .all()
    ]
    recent_records = (
        s.query(Record).order_by(Record.ts.desc()).limit(5).all()
    )

    return render_template(
        "index.html",
        monthly_budget=cfg.monthly_budget,
        is_demo=cfg.is_demo,
        record_saved=request.args.get("saved") == "1",
        over_flag=request.args.get("over") == "1",
        over_amount=request.args.get("over_amount", 0),
        no_data=no_data,
        existing_categories=cats,
        recent_records=recent_records,
    )


@app.route("/start_demo")
def start_demo():
    """现在基本不会用了（演示模式首启自动生成），保留一个备用入口即可"""
    s = Session()
    generate_demo_data(s)
    return redirect(url_for("index"))


@app.route("/exit_demo", methods=["POST"])
def exit_demo():
    s = Session()
    cfg = get_settings(s)
    if cfg.is_demo:
        s.query(Record).delete()
        cfg.is_demo = False
        cfg.monthly_budget = 0
        s.commit()
        try:
            predictor.train(s, None)
        except Exception:
            pass
        socketio.emit("update_pie")
    return redirect(url_for("index"))


@app.route("/records")
def records_page():
    s = Session()
    q = s.query(Record)

    start = request.args.get("start_date")
    end = request.args.get("end_date")
    type_ = request.args.get("type", "").strip()
    category = request.args.get("category", "").strip()
    sort = request.args.get("sort", "time_desc")

    if start:
        try:
            q = q.filter(Record.ts >= datetime.strptime(start, "%Y-%m-%d"))
        except Exception:
            pass
    if end:
        try:
            q = q.filter(
                Record.ts
                <= datetime.strptime(end, "%Y-%m-%d").replace(
                    hour=23, minute=59
                )
            )
        except Exception:
            pass

    if type_ in ("income", "expense"):
        q = q.filter(Record.type == type_)
    if category:
        q = q.filter(Record.category == category)

    if sort == "time_asc":
        q = q.order_by(Record.ts.asc())
    elif sort == "amount_desc":
        q = q.order_by(Record.amount.desc())
    elif sort == "amount_asc":
        q = q.order_by(Record.amount.asc())
    else:
        q = q.order_by(Record.ts.desc())

    page = request.args.get("page", 1, type=int)
    per_page = 20
    total_count = q.count()
    total_pages = math.ceil(total_count / per_page) if total_count else 1

    if page < 1:
        page = 1
    if page > total_pages and total_pages > 0:
        page = total_pages

    records = (
        q.offset((page - 1) * per_page).limit(per_page).all()
        if total_count
        else []
    )
    cats = [
        r[0]
        for r in s.query(Record.category)
        .filter(Record.category != None)  # noqa: E711
        .distinct()
        .all()
    ]

    # 分页链接参数
    query_params = request.args.to_dict()
    query_params.pop("page", None)
    query_params = {k: v for k, v in query_params.items() if v}

    return render_template(
        "records.html",
        records=records,
        existing_categories=cats,
        current_page=page,
        total_pages=total_pages,
        total_count=total_count,
        query_params=query_params,
    )


@app.route("/report/annual")
def annual_report():
    s = Session()
    year = request.args.get("year", datetime.now().year, type=int)

    start = datetime(year, 1, 1)
    end = datetime(year, 12, 31, 23, 59, 59)
    total_expense = (
        s.query(func.sum(Record.amount))
        .filter(
            Record.type == "expense", Record.ts >= start, Record.ts <= end
        )
        .scalar()
        or 0
    )
    total_income = (
        s.query(func.sum(Record.amount))
        .filter(
            Record.type == "income", Record.ts >= start, Record.ts <= end
        )
        .scalar()
        or 0
    )
    balance = total_income - total_expense

    max_rec = (
        s.query(Record)
        .filter(
            Record.type == "expense", Record.ts >= start, Record.ts <= end
        )
        .order_by(Record.amount.desc())
        .first()
    )
    max_expense_val = max_rec.amount if max_rec else 0
    max_expense_note = max_rec.note if max_rec else "无"

    cat_summary = (
        s.query(Record.category, func.sum(Record.amount))
        .filter(
            Record.type == "expense", Record.ts >= start, Record.ts <= end
        )
        .group_by(Record.category)
        .order_by(func.sum(Record.amount).desc())
        .all()
    )

    top_4_stats = []
    for cat, amt in cat_summary[:4]:
        top_4_stats.append({"name": cat, "value": amt})
    while len(top_4_stats) < 4:
        top_4_stats.append({"name": "---", "value": 0})

    years_list = []
    amounts_list = []
    for y in range(year - 4, year + 1):
        y_start = datetime(y, 1, 1)
        y_end = datetime(y, 12, 31, 23, 59, 59)
        y_total = (
            s.query(func.sum(Record.amount))
            .filter(
                Record.type == "expense",
                Record.ts >= y_start,
                Record.ts <= y_end,
            )
            .scalar()
            or 0
        )
        years_list.append(str(y))
        amounts_list.append(round(y_total, 2))

    pie_data = []
    top_5_sum = 0
    for cat, amt in cat_summary[:5]:
        pie_data.append({"name": cat, "value": round(amt, 2)})
        top_5_sum += amt

    other_sum = total_expense - top_5_sum
    if other_sum > 0:
        pie_data.append({"name": "其他", "value": round(other_sum, 2)})

    return render_template(
        "annual_report.html",
        year=year,
        total_expense=total_expense,
        total_income=total_income,
        balance=balance,
        max_expense_val=max_expense_val,
        max_expense_note=max_expense_note,
        top_4_stats=top_4_stats,
        years_list=years_list,
        amounts_list=amounts_list,
        pie_data=pie_data,
    )


# ===== 记录增删改 =====
@app.route("/records/new", methods=["POST"])
def records_new():
    s = Session()
    rtype = request.form.get("type")
    try:
        amount = float(request.form.get("amount"))
    except Exception:
        return redirect(url_for("index"))

    note = request.form.get("note", "").strip()
    manual_cat = request.form.get("category", "").strip()
    cat = (
        "收入"
        if rtype == "income"
        else (manual_cat if manual_cat else predictor.predict(note))
    )

    s.add(
        Record(
            ts=datetime.now(),
            amount=amount,
            type=rtype,
            category=cat,
            note=note,
        )
    )
    s.commit()

    if rtype == "expense":
        try:
            predictor.train(s, None)
        except Exception:
            pass

    socketio.emit("update_pie")
    return redirect(url_for("index", saved="1"))


@app.route("/records/update", methods=["POST"])
def records_update():
    s = Session()
    rec_id = request.form.get("id")
    rec = s.query(Record).get(rec_id)
    if rec:
        try:
            rec.amount = float(request.form.get("amount"))
        except Exception:
            pass
        rec.category = request.form.get("category", "").strip()
        rec.note = request.form.get("note", "").strip()
        s.commit()
        if rec.type == "expense":
            try:
                predictor.train(s, None)
            except Exception:
                pass
        socketio.emit("update_pie")
    return redirect(url_for("records_page"))


@app.route("/records/delete", methods=["POST"])
def records_delete():
    s = Session()
    ids = [
        int(x) for x in request.form.getlist("selected_ids") if x.isdigit()
    ]
    if ids:
        s.query(Record).filter(Record.id.in_(ids)).delete(
            synchronize_session=False
        )
        s.commit()
        try:
            predictor.train(s, None)
        except Exception:
            pass
        socketio.emit("update_pie")
    return redirect(url_for("records_page"))


# ===== API：AI 分类 / 预算 / 统计 / 导入导出 =====
@app.route("/api/predict", methods=["POST"])
def api_predict():
    data = request.json or {}
    return jsonify({"category": predictor.predict(data.get("note", ""))})


@app.route("/api/update_budget", methods=["POST"])
def api_update_budget():
    try:
        data = request.json or {}
        new_budget = float(data.get("budget", 0))
        s = Session()
        cfg = get_settings(s)
        cfg.monthly_budget = new_budget
        s.commit()
        socketio.emit("update_pie")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/stats_trend")
def api_stats_trend():
    s = Session()
    mode = request.args.get("mode", "month")
    date_str = request.args.get("date")
    try:
        target_date = (
            datetime.strptime(date_str, "%Y-%m")
            if date_str
            else datetime.now()
        )
    except Exception:
        target_date = datetime.now()

    if mode == "year":
        start = target_date.replace(
            month=1, day=1, hour=0, minute=0, second=0
        )
        end = target_date.replace(
            month=12, day=31, hour=23, minute=59, second=59
        )
        q = (
            s.query(
                extract("month", Record.ts).label("key"),
                func.sum(Record.amount).label("total"),
            )
            .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
            .group_by("key")
            .all()
        )
        data_map = {k: v for k, v in q}
        labels = [f"{m}月" for m in range(1, 13)]
        values = [round(data_map.get(m, 0) or 0, 2) for m in range(1, 13)]
        title = f"{target_date.year}年 支出趋势"
    else:
        _, days_in_month = calendar.monthrange(
            target_date.year, target_date.month
        )
        start, end = get_month_range(target_date)
        q = (
            s.query(
                extract("day", Record.ts).label("key"),
                func.sum(Record.amount).label("total"),
            )
            .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
            .group_by("key")
            .all()
        )
        data_map = {k: v for k, v in q}
        labels = [f"{d}日" for d in range(1, days_in_month + 1)]
        values = [
            round(data_map.get(d, 0) or 0, 2)
            for d in range(1, days_in_month + 1)
        ]
        title = f"{target_date.year}年{target_date.month}月 支出趋势"

    return jsonify({"labels": labels, "values": values, "title": title})


@app.route("/api/stats_category")
def api_stats_category():
    s = Session()
    date_str = request.args.get("date")
    try:
        target_date = (
            datetime.strptime(date_str, "%Y-%m")
            if date_str
            else datetime.now()
        )
    except Exception:
        target_date = datetime.now()
    start, end = get_month_range(target_date)
    q = (
        s.query(Record.category, func.sum(Record.amount))
        .filter(
            Record.type == "expense",
            Record.ts >= start,
            Record.ts <= end,
        )
        .group_by(Record.category)
        .all()
    )
    return jsonify(
        [
            {"name": c or "未分类", "value": float(v or 0)}
            for c, v in q
        ]
    )


@app.route("/api/summary")
def api_summary():
    s = Session()
    date_str = request.args.get("date")
    try:
        target_date = (
            datetime.strptime(date_str, "%Y-%m")
            if date_str
            else datetime.now()
        )
    except Exception:
        target_date = datetime.now()

    start, end = get_month_range(target_date)
    total = (
        s.query(func.sum(Record.amount))
        .filter(
            Record.type == "expense",
            Record.ts >= start,
            Record.ts <= end,
        )
        .scalar()
        or 0.0
    )
    cfg = get_settings(s)

    display_budget = (
        f"{cfg.monthly_budget:.2f}" if cfg.monthly_budget > 0 else "/"
    )
    status_text = ""
    status_class = ""

    if cfg.monthly_budget > 0:
        pct = total / cfg.monthly_budget * 100 if cfg.monthly_budget else 0
        if pct >= 100:
            status_text = "已超支！"
            status_class = (
                "badge bg-danger-subtle text-danger border border-danger-subtle"
            )
        elif pct >= 80:
            status_text = "预警"
            status_class = (
                "badge bg-warning-subtle text-warning border border-warning-subtle"
            )
        else:
            status_text = "预算正常"
            status_class = (
                "badge bg-success-subtle text-success border border-success-subtle"
            )
    else:
        status_text = "未设预算，点击设置"
        status_class = (
            "badge bg-white text-primary border border-primary border-dashed shadow-sm"
        )

    date_title = f"{target_date.year}年{target_date.month}月"
    return jsonify(
        {
            "display_spent": f"{total:.2f}",
            "display_budget": display_budget,
            "status_text": status_text,
            "status_class": status_class,
            "date_title": date_title,
        }
    )


@app.route("/api/import_data", methods=["POST"])
def api_import_data():
    s = Session()
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "msg": "未找到上传文件"})

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"success": False, "msg": "文件格式不支持"})

        # 读取文件
        if file.filename.endswith(".csv"):
            try:
                df = pd.read_csv(file)
            except UnicodeDecodeError:
                file.seek(0)
                df = pd.read_csv(file, encoding="gbk")
        else:
            df = pd.read_excel(file)

        # 智能列名映射
        col_map = {
            "时间": "ts",
            "日期": "ts",
            "交易时间": "ts",
            "Date": "ts",
            "金额": "amount",
            "交易金额": "amount",
            "Price": "amount",
            "分类": "category",
            "类别": "category",
            "Category": "category",
            "备注": "note",
            "商品说明": "note",
            "Note": "note",
            "类型": "type",
            "收支": "type",
        }

        df.rename(columns=lambda x: col_map.get(str(x).strip(), x), inplace=True)

        if "amount" not in df.columns:
            return jsonify({"success": False, "msg": "无法识别'金额'列"})

        # 默认值
        if "ts" not in df.columns:
            df["ts"] = datetime.now()
        if "category" not in df.columns:
            df["category"] = "导入数据"
        if "note" not in df.columns:
            df["note"] = "批量导入"
        if "type" not in df.columns:
            df["type"] = "expense"

        count = 0
        new_records = []
        for _, row in df.iterrows():
            try:
                ts_val = pd.to_datetime(row["ts"])
            except Exception:
                ts_val = datetime.now()

            try:
                amt_val = float(row["amount"])
            except Exception:
                continue

            type_str = str(row["type"]).strip()
            final_type = (
                "income" if type_str in ["收入", "Income", "INCOME"] else "expense"
            )

            note_val = str(row["note"]) if pd.notna(row["note"]) else ""
            cat_val = (
                str(row["category"])
                if pd.notna(row["category"])
                else "其他"
            )

            new_records.append(
                Record(
                    ts=ts_val,
                    amount=abs(amt_val),
                    type=final_type,
                    category=cat_val,
                    note=note_val,
                )
            )
            count += 1

        if new_records:
            s.add_all(new_records)
            s.commit()
            try:
                predictor.train(s, None)
            except Exception:
                pass
            socketio.emit("update_pie")
            return jsonify({"success": True, "count": count})
        else:
            return jsonify({"success": False, "msg": "未发现有效数据"})

    except Exception as e:
        return jsonify({"success": False, "msg": f"导入错误: {str(e)}"})


@app.route("/api/export_excel", methods=["POST"])
def api_export_excel():
    try:
        s = Session()
        records = s.query(Record).order_by(Record.ts.desc()).all()

        if not records:
            return jsonify({"success": False, "msg": "暂无数据可导出"})

        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        filename = f"账单备份_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        filepath = os.path.join(desktop, filename)

        data = [
            {
                "时间": r.ts,
                "类型": "收入" if r.type == "income" else "支出",
                "金额": r.amount,
                "类别": r.category,
                "备注": r.note,
            }
            for r in records
        ]

        df = pd.DataFrame(data)
        df.to_excel(filepath, index=False)

        if platform.system() == "Windows":
            try:
                subprocess.Popen(f'explorer /select,"{filepath}"')
            except Exception:
                pass

        return jsonify({"success": True, "path": filepath})

    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})


@app.route("/open_data_folder")
def open_data_folder():
    """打开数据目录；现在不再依赖 settings 页面，直接回首页"""
    path = get_data_path()
    try:
        if platform.system() == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        flash(f"已打开数据目录: {path}", "success")
    except Exception:
        flash(f"打开失败: {path}", "danger")
    return redirect(url_for("index"))


# ===== Session 清理 =====
@app.teardown_appcontext
def shutdown_session(exception=None):
    Session.remove()


if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=5000, debug=True)
