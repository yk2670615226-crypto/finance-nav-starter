import calendar
import math
import os
import platform
import random
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple

import pandas as pd
from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import extract, func

from ai_service import predictor
from models import AppSettings, Record
from webapp import socketio
from webapp.config import get_data_path
from webapp.db import get_db_session

bp = Blueprint("main", __name__)

MAX_IMPORT_SIZE = 5 * 1024 * 1024  # 5MB
MAX_EXPORT_ROWS = 50000


def get_month_range(target_date: datetime) -> Tuple[datetime, datetime]:
    start = target_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    _, last_day = calendar.monthrange(start.year, start.month)
    end = start.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def train_model_async() -> None:
    def _train_task():
        with current_app.app_context():
            with get_db_session(current_app) as db_session:
                try:
                    predictor.train(db_session)
                except Exception as exc:  # pragma: no cover - logging only
                    print(f"后台训练失败: {exc}")

    threading.Thread(target=_train_task, daemon=True).start()


def generate_demo_data(session_obj, target_records: int = 10000, years: int = 10) -> None:
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
            Record(ts=ts, amount=amount, type=rtype, category=category, note=note)
        )

    session_obj.add_all(records)

    expense_sum = sum(r.amount for r in records if r.type == "expense")
    months_count = years * 12
    avg_monthly = expense_sum / months_count if months_count else 5000
    base_budget = round(avg_monthly * 1.15 / 100) * 100

    cfg = session_obj.query(AppSettings).first()
    if not cfg:
        cfg = AppSettings()
        session_obj.add(cfg)
    cfg.monthly_budget = base_budget
    cfg.enable_lock = False
    cfg.is_demo = True
    session_obj.commit()

    train_model_async()


def get_settings(db_session):
    cfg = db_session.query(AppSettings).first()
    if not cfg:
        cfg = AppSettings(monthly_budget=8000.0, enable_lock=False, is_demo=True)
        db_session.add(cfg)
        generate_demo_data(db_session)
        cfg = db_session.query(AppSettings).first()
    return cfg


def get_budget_status(total: float, budget: float) -> Tuple[str, str]:
    st_text = "预算正常"
    st_cls = "badge bg-success-subtle text-success border-success-subtle"

    if budget > 0:
        pct = (total / budget) * 100
        if pct >= 100:
            st_text, st_cls = "已超支！", "badge bg-danger-subtle text-danger border-danger-subtle"
        elif pct >= 80:
            st_text, st_cls = "预警", "badge bg-warning-subtle text-warning border-warning-subtle"
    else:
        st_text, st_cls = "未设预算", "badge bg-light text-primary border-primary border-dashed"

    return st_text, st_cls


@bp.app_context_processor
def inject_config():
    with get_db_session() as db_session:
        return {"config": get_settings(db_session)}


@bp.before_app_request
def check_app_lock():
    if request.endpoint in ["static", "main.lock_screen", "main.do_unlock"]:
        return None

    with get_db_session() as db_session:
        cfg = get_settings(db_session)
        if cfg.enable_lock and not session.get("is_unlocked"):
            return redirect(url_for("main.lock_screen"))
    return None


@bp.teardown_app_request
def shutdown_session(exception=None):
    session_factory = current_app.config.get("SessionFactory")
    if session_factory:
        session_factory.remove()


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
        cfg = get_settings(db_session)
        if cfg.check_lock_password(password):
            session["is_unlocked"] = True
            return redirect(url_for("main.index"))

    flash("密码错误", "danger")
    return redirect(url_for("main.lock_screen"))


@bp.post("/lock/update")
def lock_update():
    data = request.form or (request.json or {})
    action = data.get("action", "").strip()

    with get_db_session() as db_session:
        cfg = get_settings(db_session)

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
            session.pop("is_unlocked", None)
            flash("已关闭应用锁", "success")
    return redirect(request.referrer or url_for("main.index"))


@bp.route("/")
def index():
    now = datetime.now()
    start, end = get_month_range(now)

    with get_db_session() as db_session:
        cfg = get_settings(db_session)
        total_expense = (
            db_session.query(func.sum(Record.amount))
            .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
            .scalar()
            or 0.0
        )

        cats = [
            r[0]
            for r in db_session.query(Record.category)
            .filter(Record.category.isnot(None))
            .distinct()
        ]
        recent = db_session.query(Record).order_by(Record.ts.desc()).limit(5).all()

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
        generate_demo_data(db_session)
    return redirect(url_for("main.index"))


@bp.route("/exit_demo", methods=["POST"])
def exit_demo():
    with get_db_session() as db_session:
        cfg = get_settings(db_session)
        if cfg.is_demo:
            db_session.query(Record).delete()
            cfg.is_demo = False
            cfg.monthly_budget = 0
            db_session.commit()
            train_model_async()
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
        q = db_session.query(Record)

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
        cats = [r[0] for r in db_session.query(Record.category).distinct()]

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
            .filter(Record.ts >= start, Record.ts <= end)
            .group_by(Record.type)
            .all()
        )
        sums = {t: (v or 0) for t, v in totals}

        total_exp = sums.get("expense", 0)
        total_inc = sums.get("income", 0)

        max_rec = (
            db_session.query(Record)
            .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
            .order_by(Record.amount.desc())
            .first()
        )

        cat_stats = (
            db_session.query(Record.category, func.sum(Record.amount))
            .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
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
                .filter(Record.type == "expense", Record.ts >= y_s, Record.ts <= y_e)
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

    cat = "收入" if rtype == "income" else (manual_cat if manual_cat else predictor.predict(note))

    with get_db_session() as db_session:
        db_session.add(
            Record(ts=datetime.now(), amount=amount, type=rtype, category=cat, note=note)
        )
        db_session.commit()

    if rtype == "expense":
        train_model_async()

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
        rec = db_session.query(Record).get(rec_id)
        if not rec:
            flash("记录不存在", "warning")
            return redirect(url_for("main.records_page"))

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
            train_model_async()
        socketio.emit("update_pie")
    return redirect(url_for("main.records_page"))


@bp.post("/records/delete")
def records_delete():
    ids = [int(x) for x in request.form.getlist("selected_ids") if x.isdigit()]
    if not ids:
        flash("请选择要删除的记录", "warning")
        return redirect(url_for("main.records_page"))

    with get_db_session() as db_session:
        db_session.query(Record).filter(Record.id.in_(ids)).delete(synchronize_session=False)
        db_session.commit()

    train_model_async()
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
        cfg = get_settings(db_session)
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
                db_session.query(extract("month", Record.ts).label("k"), func.sum(Record.amount))
                .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
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
                db_session.query(extract("day", Record.ts).label("k"), func.sum(Record.amount))
                .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
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
        data = (
            db_session.query(Record.category, func.sum(Record.amount))
            .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
            .group_by(Record.category)
            .all()
        )
        total = (
            db_session.query(func.sum(Record.amount))
            .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
            .scalar()
            or 0.0
        )

        cfg = get_settings(db_session)
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
            .filter(Record.type == "expense", Record.ts >= start, Record.ts <= end)
            .scalar()
            or 0.0
        )
        cfg = get_settings(db_session)
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
    if request.content_length and request.content_length > MAX_IMPORT_SIZE:
        return jsonify({"success": False, "msg": "文件过大，请压缩或分批导入（上限 5MB）"})

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"success": False, "msg": "文件无效"})

    filename = f.filename.lower()
    if not (filename.endswith(".csv") or filename.endswith(".xlsx") or filename.endswith(".xls")):
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
                )
            )

        if not recs:
            return jsonify({"success": False, "msg": "无有效数据"})

        with get_db_session() as db_session:
            db_session.add_all(recs)
            db_session.commit()

        train_model_async()
        socketio.emit("update_pie")
        return jsonify({"success": True, "count": len(recs)})

    except Exception as exc:
        return jsonify({"success": False, "msg": str(exc)})


@bp.post("/api/export_excel")
def api_export_excel():
    try:
        with get_db_session() as db_session:
            recs = db_session.query(Record).order_by(Record.ts.desc()).all()
        if not recs:
            return jsonify({"success": False, "msg": "无数据"})

        if len(recs) > MAX_EXPORT_ROWS:
            return jsonify({"success": False, "msg": "数据量过大，请筛选后再导出（上限 5 万行）"})

        desktop = Path.home() / "Desktop"
        name = f"账单备份_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        path = desktop / name

        desktop.mkdir(parents=True, exist_ok=True)

        data = [
            {
                "时间": r.ts,
                "类型": ("收入" if r.type == "income" else "支出"),
                "金额": r.amount,
                "类别": r.category,
                "备注": r.note,
            }
            for r in recs
        ]

        pd.DataFrame(data).to_excel(path, index=False)

        if platform.system() == "Windows":
            subprocess.run(["explorer", "/select,", str(path)], check=False)

        return jsonify({"success": True, "path": str(path)})
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
