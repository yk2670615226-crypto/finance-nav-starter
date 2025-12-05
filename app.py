import os
import sys
import random
import calendar
import math
import platform
import subprocess
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple

import pandas as pd

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, flash, session
)
from flask_socketio import SocketIO
from sqlalchemy import create_engine, func, extract
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import NullPool

from models import Base, Record, AppSettings
from ai_service import predictor


# ===== 路径配置 =====
def get_base_path() -> Path:
    """获取资源路径（适配 PyInstaller 打包）"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) # type: ignore
    return Path(__file__).parent.absolute()


def get_data_path() -> Path:
    """获取数据存储路径（适配各操作系统）"""
    if getattr(sys, "frozen", False):
        home = Path.home()
        system = platform.system()
        if system == "Windows":
            base = home / "AppData" / "Roaming"
        elif system == "Darwin":
            base = home / "Library" / "Application Support"
        else:
            base = home / ".local" / "share"
        
        data_dir = base / "MyAccounting"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    else:
        # 开发模式下存放在当前目录
        return Path(__file__).parent.absolute()


BASE_DIR = get_base_path()
DATA_DIR = get_data_path()
DB_PATH = DATA_DIR / "finance.db"

print(f"数据库路径: {DB_PATH}")

TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
# 动态生成密钥，保障 Session 安全
app.config["SECRET_KEY"] = os.urandom(24).hex()
socketio = SocketIO(app, cors_allowed_origins="*")

# 数据库连接
engine = create_engine(
    f"sqlite:///{DB_PATH}?check_same_thread=False",
    poolclass=NullPool,
    future=True,
)
Base.metadata.create_all(engine)
Session = scoped_session(sessionmaker(bind=engine))


# ===== 核心辅助函数 =====

def get_month_range(target_date: datetime) -> Tuple[datetime, datetime]:
    """获取指定日期的当月起止时间"""
    start = target_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    _, last_day = calendar.monthrange(start.year, start.month)
    end = start.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def train_model_async():
    """
    【核心优化】后台异步训练模型
    避免在保存记录时阻塞 UI 线程。
    """
    def _train_task():
        # 在新线程中必须创建新的数据库会话
        with app.app_context():
            s = Session()
            try:
                predictor.train(s)
            except Exception as e:
                print(f"后台训练失败: {e}")
            finally:
                s.close()
    
    threading.Thread(target=_train_task, daemon=True).start()


def generate_demo_data(session_obj, target_records: int = 10000, years: int = 10):
    """生成演示数据（包含性能优化后的批量插入）"""
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

    # 缓存随机因子，加速循环
    month_factors = {}

    for _ in range(target_records):
        day_offset = random.randint(0, total_days)
        current_day = start_date + timedelta(days=day_offset)
        ts = current_day.replace(
            hour=random.randint(8, 22),
            minute=random.randint(0, 59),
            second=random.randint(0, 59)
        )

        # 随机消费波动
        m_key = (current_day.year, current_day.month)
        if m_key not in month_factors:
            month_factors[m_key] = random.uniform(0.8, 1.3)
        m_factor = month_factors[m_key]

        # 生成记录
        if random.random() < 0.12: # 收入
            rtype = "income"
            category = "收入"
            if current_day.day in (10, 15, 25) and random.random() < 0.6:
                note = "工资"
                amount = round(random.uniform(8000, 15000), 2)
            else:
                note = random.choice(income_notes)
                amount = round(random.uniform(100, 2000), 2)
        else: # 支出
            rtype = "expense"
            cat_key = random.choice(list(categories.keys()))
            category = cat_key
            note = random.choice(categories[cat_key])
            
            base_amt = random.uniform(10, 300)
            if cat_key in ["购物", "教育", "人情"]: base_amt = random.uniform(50, 1000)
            
            day_factor = 1.2 if current_day.weekday() >= 5 else 1.0
            amount = round(base_amt * day_factor * m_factor, 2)

        records.append(Record(ts=ts, amount=amount, type=rtype, category=category, note=note))

    # 批量写入
    session_obj.add_all(records)

    # 估算预算
    expense_sum = sum(r.amount for r in records if r.type == 'expense')
    months_count = years * 12
    avg_monthly = expense_sum / months_count if months_count else 5000
    base_budget = round(avg_monthly * 1.15 / 100) * 100

    # 更新设置
    cfg = session_obj.query(AppSettings).first()
    if not cfg:
        cfg = AppSettings()
        session_obj.add(cfg)
    cfg.monthly_budget = base_budget
    cfg.enable_lock = False
    cfg.is_demo = True
    session_obj.commit()

    # 触发异步训练
    train_model_async()


def get_settings(s):
    """获取配置，首次启动自动初始化"""
    cfg = s.query(AppSettings).first()
    if not cfg:
        cfg = AppSettings(monthly_budget=8000.0, enable_lock=False, is_demo=True)
        s.add(cfg)
        generate_demo_data(s)
        cfg = s.query(AppSettings).first()
    return cfg


# ===== 中间件 =====
@app.context_processor
def inject_config():
    s = Session()
    return {"config": get_settings(s)}

@app.before_request
def check_app_lock():
    if request.endpoint in ["static", "lock_screen", "do_unlock"]:
        return
    s = Session()
    cfg = get_settings(s)
    if cfg.enable_lock and not session.get("is_unlocked"):
        return redirect(url_for("lock_screen"))

@app.teardown_appcontext
def shutdown_session(exception=None):
    Session.remove()


# ===== 路由：锁屏 =====
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
    s = Session()
    cfg = get_settings(s)
    data = request.form or (request.json or {})
    action = data.get("action", "").strip()

    if action == "lock_enable":
        pwd = data.get("password", "").strip()
        if pwd:
            cfg.set_lock_password(pwd)
            cfg.enable_lock = True
            s.commit()
            flash("已开启应用锁", "success")
        else:
            flash("密码不能为空", "danger")
    elif action == "lock_disable":
        cfg.enable_lock = False
        s.commit()
        session.pop("is_unlocked", None)
        flash("已关闭应用锁", "success")
    return redirect(request.referrer or url_for("index"))


# ===== 路由：主页与记录 =====
@app.route("/")
def index():
    s = Session()
    cfg = get_settings(s)
    now = datetime.now()
    start, end = get_month_range(now)

    total_expense = s.query(func.sum(Record.amount)).filter(
        Record.type == "expense", Record.ts >= start, Record.ts <= end
    ).scalar() or 0.0

    cats = [r[0] for r in s.query(Record.category).filter(Record.category.isnot(None)).distinct()]
    recent = s.query(Record).order_by(Record.ts.desc()).limit(5).all()

    return render_template(
        "index.html",
        monthly_budget=cfg.monthly_budget,
        is_demo=cfg.is_demo,
        record_saved=request.args.get("saved") == "1",
        no_data=(cfg.monthly_budget == 0 and total_expense == 0),
        existing_categories=cats,
        recent_records=recent,
    )

@app.route("/start_demo")
def start_demo():
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
        train_model_async()
        socketio.emit("update_pie")
    return redirect(url_for("index"))

@app.route("/records")
def records_page():
    s = Session()
    q = s.query(Record)

    start = request.args.get("start_date")
    end = request.args.get("end_date")
    rtype = request.args.get("type", "").strip()
    cat = request.args.get("category", "").strip()
    sort = request.args.get("sort", "time_desc")

    if start:
        try: q = q.filter(Record.ts >= datetime.strptime(start, "%Y-%m-%d"))
        except: pass
    if end:
        try: q = q.filter(Record.ts <= datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59))
        except: pass
    if rtype in ("income", "expense"):
        q = q.filter(Record.type == rtype)
    if cat:
        q = q.filter(Record.category == cat)

    sort_map = {
        "time_asc": Record.ts.asc(),
        "time_desc": Record.ts.desc(),
        "amount_asc": Record.amount.asc(),
        "amount_desc": Record.amount.desc()
    }
    q = q.order_by(sort_map.get(sort, Record.ts.desc()))

    page = request.args.get("page", 1, type=int)
    per_page = 20
    total = q.count()
    pages = math.ceil(total / per_page) if total else 1
    page = max(1, min(page, pages))

    records = q.offset((page - 1) * per_page).limit(per_page).all() if total else []
    cats = [r[0] for r in s.query(Record.category).distinct()]

    q_params = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template(
        "records.html",
        records=records,
        existing_categories=cats,
        current_page=page,
        total_pages=pages,
        total_count=total,
        query_params=q_params
    )

@app.route("/report/annual")
def annual_report():
    s = Session()
    year = request.args.get("year", datetime.now().year, type=int)
    start = datetime(year, 1, 1)
    end = datetime(year, 12, 31, 23, 59, 59)

    totals = s.query(Record.type, func.sum(Record.amount)).filter(
        Record.ts >= start, Record.ts <= end
    ).group_by(Record.type).all()
    sums = {t: (v or 0) for t, v in totals}
    
    total_exp = sums.get("expense", 0)
    total_inc = sums.get("income", 0)

    max_rec = s.query(Record).filter(
        Record.type == "expense", Record.ts >= start, Record.ts <= end
    ).order_by(Record.amount.desc()).first()

    cat_stats = s.query(Record.category, func.sum(Record.amount)).filter(
        Record.type == "expense", Record.ts >= start, Record.ts <= end
    ).group_by(Record.category).order_by(func.sum(Record.amount).desc()).all()

    top_4 = [{"name": c, "value": v} for c, v in cat_stats[:4]]
    while len(top_4) < 4: top_4.append({"name": "---", "value": 0})

    # 趋势
    years_list = []
    amounts_list = []
    for y in range(year - 4, year + 1):
        y_s = datetime(y, 1, 1)
        y_e = datetime(y, 12, 31, 23, 59, 59)
        val = s.query(func.sum(Record.amount)).filter(
            Record.type == "expense", Record.ts >= y_s, Record.ts <= y_e
        ).scalar() or 0
        years_list.append(str(y))
        amounts_list.append(round(val, 2))

    pie_data = [{"name": c, "value": round(v, 2)} for c, v in cat_stats[:5]]
    other = total_exp - sum(x["value"] for x in pie_data)
    if other > 0: pie_data.append({"name": "其他", "value": round(other, 2)})

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
        pie_data=pie_data
    )


# ===== 记录操作 =====
@app.post("/records/new")
def records_new():
    s = Session()
    try:
        amount = float(request.form.get("amount", 0))
    except:
        return redirect(url_for("index"))

    rtype = request.form.get("type")
    note = request.form.get("note", "").strip()
    manual_cat = request.form.get("category", "").strip()
    
    cat = "收入" if rtype == "income" else (manual_cat if manual_cat else predictor.predict(note))

    s.add(Record(ts=datetime.now(), amount=amount, type=rtype, category=cat, note=note))
    s.commit()

    # 异步训练，解决卡顿
    if rtype == "expense":
        train_model_async()

    socketio.emit("update_pie")
    return redirect(url_for("index", saved="1"))

@app.post("/records/update")
def records_update():
    s = Session()
    rec = s.query(Record).get(request.form.get("id"))
    if rec:
        try: rec.amount = float(request.form.get("amount"))
        except: pass
        rec.category = request.form.get("category", "").strip()
        rec.note = request.form.get("note", "").strip()
        s.commit()
        if rec.type == "expense":
            train_model_async()
        socketio.emit("update_pie")
    return redirect(url_for("records_page"))

@app.post("/records/delete")
def records_delete():
    s = Session()
    ids = [int(x) for x in request.form.getlist("selected_ids") if x.isdigit()]
    if ids:
        s.query(Record).filter(Record.id.in_(ids)).delete(synchronize_session=False)
        s.commit()
        train_model_async()
        socketio.emit("update_pie")
    return redirect(url_for("records_page"))


# ===== API =====
@app.post("/api/predict")
def api_predict():
    data = request.json or {}
    return jsonify({"category": predictor.predict(data.get("note", ""))})

@app.post("/api/update_budget")
def api_update_budget():
    try:
        val = float((request.json or {}).get("budget", 0))
        s = Session()
        cfg = get_settings(s)
        cfg.monthly_budget = val
        s.commit()
        socketio.emit("update_pie")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/stats_trend")
def api_stats_trend():
    s = Session()
    mode = request.args.get("mode", "month")
    try: target = datetime.strptime(request.args.get("date", ""), "%Y-%m")
    except: target = datetime.now()

    if mode == "year":
        start = target.replace(month=1, day=1, hour=0, minute=0, second=0)
        end = target.replace(month=12, day=31, hour=23, minute=59, second=59)
        q = s.query(extract("month", Record.ts).label("k"), func.sum(Record.amount)).filter(
            Record.type == "expense", Record.ts >= start, Record.ts <= end
        ).group_by("k").all()
        data = {k: v for k, v in q}
        labels = [f"{m}月" for m in range(1, 13)]
        values = [round(data.get(m, 0) or 0, 2) for m in range(1, 13)]
        title = f"{target.year}年 支出趋势"
    else:
        start, end = get_month_range(target)
        _, days = calendar.monthrange(target.year, target.month)
        q = s.query(extract("day", Record.ts).label("k"), func.sum(Record.amount)).filter(
            Record.type == "expense", Record.ts >= start, Record.ts <= end
        ).group_by("k").all()
        data = {k: v for k, v in q}
        labels = [f"{d}日" for d in range(1, days + 1)]
        values = [round(data.get(d, 0) or 0, 2) for d in range(1, days + 1)]
        title = f"{target.year}年{target.month}月 支出趋势"

    return jsonify({"labels": labels, "values": values, "title": title})

@app.route("/api/stats_category")
def api_stats_category():
    s = Session()
    try: target = datetime.strptime(request.args.get("date", ""), "%Y-%m")
    except: target = datetime.now()
    start, end = get_month_range(target)
    
    q = s.query(Record.category, func.sum(Record.amount)).filter(
        Record.type == "expense", Record.ts >= start, Record.ts <= end
    ).group_by(Record.category).all()
    
    return jsonify([{"name": c or "未分类", "value": float(v or 0)} for c, v in q])

@app.route("/api/summary")
def api_summary():
    s = Session()
    try: target = datetime.strptime(request.args.get("date", ""), "%Y-%m")
    except: target = datetime.now()
    start, end = get_month_range(target)
    
    total = s.query(func.sum(Record.amount)).filter(
        Record.type == "expense", Record.ts >= start, Record.ts <= end
    ).scalar() or 0.0
    
    cfg = get_settings(s)
    bg = cfg.monthly_budget
    
    st_text = "预算正常"
    st_cls = "badge bg-success-subtle text-success border-success-subtle"
    
    if bg > 0:
        pct = (total / bg) * 100
        if pct >= 100:
            st_text, st_cls = "已超支！", "badge bg-danger-subtle text-danger border-danger-subtle"
        elif pct >= 80:
            st_text, st_cls = "预警", "badge bg-warning-subtle text-warning border-warning-subtle"
    else:
        st_text, st_cls = "未设预算", "badge bg-light text-primary border-primary border-dashed"
        
    return jsonify({
        "display_spent": f"{total:.2f}",
        "display_budget": f"{bg:.2f}" if bg > 0 else "/",
        "status_text": st_text,
        "status_class": st_cls,
        "date_title": f"{target.year}年{target.month}月"
    })

@app.post("/api/import_data")
def api_import_data():
    s = Session()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"success": False, "msg": "文件无效"})
    
    try:
        if f.filename.endswith(".csv"):
            try: df = pd.read_csv(f)
            except: 
                f.seek(0)
                df = pd.read_csv(f, encoding="gbk")
        else:
            df = pd.read_excel(f)
            
        col_map = {
            "时间": "ts", "日期": "ts", "Date": "ts",
            "金额": "amount", "Price": "amount",
            "分类": "category", "类别": "category",
            "备注": "note", "说明": "note",
            "类型": "type", "收支": "type"
        }
        df.rename(columns=lambda x: col_map.get(str(x).strip(), x), inplace=True)
        
        if "amount" not in df.columns:
            return jsonify({"success": False, "msg": "缺少金额列"})
        
        df["ts"] = pd.to_datetime(df.get("ts", datetime.now()), errors='coerce').fillna(datetime.now())
        df["category"] = df.get("category", "导入").fillna("其他").astype(str)
        df["note"] = df.get("note", "").fillna("").astype(str)
        
        recs = []
        for _, row in df.iterrows():
            try: amt = float(row["amount"])
            except: continue
            
            # 智能推断类型
            tp = str(row.get("type", "")).strip()
            if tp in ["收入", "income", "Income"]: 
                final_tp = "income"
            else:
                final_tp = "expense"
            
            recs.append(Record(
                ts=row["ts"], amount=abs(amt), type=final_tp, 
                category=row["category"], note=row["note"]
            ))
            
        if recs:
            s.add_all(recs)
            s.commit()
            train_model_async()
            socketio.emit("update_pie")
            return jsonify({"success": True, "count": len(recs)})
        
        return jsonify({"success": False, "msg": "无有效数据"})
        
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

@app.post("/api/export_excel")
def api_export_excel():
    try:
        s = Session()
        recs = s.query(Record).order_by(Record.ts.desc()).all()
        if not recs: return jsonify({"success": False, "msg": "无数据"})
        
        desktop = Path.home() / "Desktop"
        name = f"账单备份_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        path = desktop / name
        
        data = [{
            "时间": r.ts, "类型": ("收入" if r.type=="income" else "支出"),
            "金额": r.amount, "类别": r.category, "备注": r.note
        } for r in recs]
        
        pd.DataFrame(data).to_excel(path, index=False)
        
        if platform.system() == "Windows":
            subprocess.run(['explorer', '/select,', str(path)], check=False)
            
        return jsonify({"success": True, "path": str(path)})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

@app.route("/open_data_folder")
def open_data_folder():
    path = str(get_data_path())
    try:
        if platform.system() == "Windows": os.startfile(path)
        elif platform.system() == "Darwin": subprocess.Popen(["open", path])
        else: subprocess.Popen(["xdg-open", path])
        flash(f"已打开: {path}", "success")
    except:
        flash("打开失败", "danger")
    return redirect(url_for("index"))

if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=5000, debug=True)