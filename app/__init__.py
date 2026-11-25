"""Flask 核账页应用工厂（支持同期多次换表接续）。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict

from flask import (Flask, Response, flash, jsonify, redirect,
                   render_template, request, url_for)

from . import service, storage
from .solver import AuditResult


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
    db_path = db_path or os.environ.get(
        "DB_PATH", os.path.join(os.getcwd(), "data", "meter.db"))
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    app.config["DB_PATH"] = db_path

    def conn():
        return storage.get_db(app.config["DB_PATH"])

    def _is_suspended(db) -> bool:
        cur = storage.latest_audit(db, "current")
        return bool(cur and cur["suspended"])

    @app.get("/")
    def index():
        db = conn()
        rows = storage.load_readings(db)
        raw = storage.load_raw_readings(db)
        cfg = storage.get_config(db)
        initial = storage.latest_audit(db, "initial")
        current = storage.latest_audit(db, "current")
        suspended = bool(current and current["suspended"])
        current_result = None if suspended else _load_result(current)
        windows = [] if suspended else service.window_rows(db, current_result)
        meters = storage.list_meters(db)
        changes = storage.list_changes(db)
        summary = storage.get_summary(db)
        spans = storage.meter_spans(db)

        lookup = None
        minute_text = request.args.get("minute", "").strip()
        minute_error = None
        if minute_text:
            try:
                ts = service.parse_minute(minute_text)
                lookup = storage.lookup_minute(db, ts)
            except (service.AuditInputError, storage.UploadError) as exc:
                minute_error = str(exc)
        return render_template(
            "index.html",
            readings=rows, raw_readings=raw, cfg=cfg,
            initial=initial, current=current, suspended=suspended,
            current_result=current_result, windows=windows,
            meters=meters, changes=changes, summary=summary, spans=spans,
            lookup=lookup, minute_text=minute_text, minute_error=minute_error)

    @app.post("/upload")
    def upload():
        file = request.files.get("csv")
        if not file or not file.filename:
            flash("请选择 CSV 文件", "error")
            return redirect(url_for("index"))
        try:
            meter_seq = int(request.form.get("meter_seq", "0"))
        except (TypeError, ValueError):
            flash("所选电表使用时段非法", "error")
            return redirect(url_for("index"))
        try:
            incoming = storage.parse_csv(file.read())
            db = conn()
            total, overwritten = storage.merge_upload(db, incoming, meter_seq)
        except storage.UploadError as exc:
            flash(f"上传被整批拒绝：{exc}", "error")
            return redirect(url_for("index"))
        flash(f"上传成功：合并后共 {total} 条原始读数"
              f"（同刻覆盖 {overwritten} 条，新增 {len(incoming) - overwritten} 条）",
              "ok")
        return redirect(url_for("index"))

    @app.post("/change")
    def change_meter():
        try:
            db = conn()
            rec = storage.register_meter_change(
                db,
                request.form.get("handover_minute", ""),
                request.form.get("new_meter_id", ""),
                request.form.get("new_start_reading", ""))
        except storage.UploadError as exc:
            flash(f"换表登记被拒绝：{exc}（接续值照旧）", "error")
            return redirect(url_for("index"))
        flash(
            "换表成功：交接分钟 "
            f"{rec['handover_minute']}，新表 {rec['new_meter_seq']} 起始 "
            f"{rec['new_start_reading']} Wh，折算统一接续值 "
            f"{rec['unified_value']} Wh；旧当前结论与 CSV 已暂停，"
            "请用接续值重新核对。", "ok")
        return redirect(url_for("index"))

    @app.get("/minute")
    def minute_lookup():
        text = request.args.get("minute", "").strip()
        try:
            ts = service.parse_minute(text)
            db = conn()
            data = storage.lookup_minute(db, ts)
        except (service.AuditInputError, storage.UploadError) as exc:
            if request.args.get("format") == "json":
                return jsonify({"error": str(exc)}), 400
            flash(f"按分钟查询失败：{exc}", "error")
            return redirect(url_for("index"))
        if request.args.get("format") == "json":
            return jsonify(data)
        return redirect(url_for("index", minute=data["minute"]))

    @app.post("/audit")
    def audit():
        try:
            db = conn()
            result = service.run_audit(
                db,
                request.form.get("per_minute_cap", ""),
                request.form.get("peak_wh", ""),
                request.form.get("reported_window", ""))
        except service.AuditInputError as exc:
            flash(f"未执行核对：{exc}（已有结论保持不变）", "error")
            return redirect(url_for("index"))
        labels = {"feasible": "核对通过",
                  "target_unreachable": "所报时段达不到峰值",
                  "others_breach": "可达但其余时段守不住上限"}
        flash(f"核对完成：{labels[result.status]} —— {result.message}",
              "ok" if result.status == "feasible" else "error")
        return redirect(url_for("index"))

    @app.get("/windows.csv")
    def windows_csv():
        db = conn()
        current = storage.latest_audit(db, "current")
        if current and current["suspended"]:
            flash("换表/更正后旧结论已暂停，CSV 暂不提供，请重新核对",
                  "error")
            return redirect(url_for("index"))
        result = _load_result(current)
        rows = service.window_rows(db, result)
        if not rows:
            flash("暂无可导出的各时段电量", "error")
            return redirect(url_for("index"))
        body = ["window_start,window_index,energy_wh,is_reported"]
        body += [f"{ts},{i},{wh},{int(flag)}" for ts, wh, i, flag in rows]
        return Response("\n".join(body) + "\n", mimetype="text/csv",
                        headers={"Content-Disposition":
                                 "attachment; filename=windows.csv"})

    @app.post("/reset")
    def reset():
        storage.reset_all(conn())
        flash("已清空账期：读数、换表记录、接续汇总、参数与全部结论", "ok")
        return redirect(url_for("index"))

    return app


def _load_result(row) -> AuditResult | None:
    if row is None:
        return None
    data = json.loads(row["detail_json"])
    return AuditResult(**data)
