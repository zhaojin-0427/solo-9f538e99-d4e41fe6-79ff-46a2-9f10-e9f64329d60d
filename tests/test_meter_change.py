"""换表接续专项自测：登记、使用时段、交接更正、整批拒绝、暂停/恢复、
按分钟查询（读数预算）与 Flask 端到端流程。
"""
import io
import os
import tempfile
import unittest
from datetime import datetime

from app import create_app, service, storage


def _csv(text):
    return text.encode("utf-8")


class MeterChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "m.db")
        self.conn = storage.get_db(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _upload(self, text, meter_seq=0):
        return storage.merge_upload(
            self.conn, storage.parse_csv(_csv(text)), meter_seq)

    def test_change_basic_continuation_and_unified_value(self):
        # 首表 00:00..00:30，00:30 末次实读分钟换表：旧表末读 60，新表从 10 起
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n2026-11-01 00:30,60\n")
        rec = storage.register_meter_change(
            self.conn, "2026-11-01 00:30", "M2", "10")
        self.assertEqual(rec["unified_value"], 60)
        # 新表补传 00:45=40（偏移 50 → 接续 90）
        total, ow = self._upload(
            "timestamp,reading\n2026-11-01 00:45,40\n", meter_seq=1)
        self.assertEqual((total, ow), (5, 0))
        # 同点重传：同刻覆盖
        total, ow = self._upload(
            "timestamp,reading\n2026-11-01 00:45,40\n", meter_seq=1)
        self.assertEqual((total, ow), (5, 1))

        series = dict(storage.load_readings(self.conn))
        self.assertEqual(series[datetime(2026, 11, 1, 0, 0)], 0)
        self.assertEqual(series[datetime(2026, 11, 1, 0, 30)], 60)
        self.assertEqual(series[datetime(2026, 11, 1, 0, 45)], 90)
        self.assertEqual(len(series), 4)  # 交接分钟两侧去重

        # 按分钟：交接分钟读到两块表的实读，接续值唯一
        info = storage.lookup_minute(
            self.conn, datetime(2026, 11, 1, 0, 30))
        self.assertEqual({r["meter_id"]: r["reading_wh"]
                          for r in info["raw_readings"]},
                         {"M1": 60, "M2": 10})
        self.assertEqual(info["continuation_value"], 60)
        self.assertIsNotNone(info["meter_change"])
        info2 = storage.lookup_minute(
            self.conn, datetime(2026, 11, 1, 0, 45))
        self.assertEqual(info2["continuation_value"], 90)

    def test_change_must_be_at_last_actual_reading_and_after_first(self):
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n2026-11-01 00:30,60\n")
        with self.assertRaises(storage.MeterChangeError):
            storage.register_meter_change(
                self.conn, "2026-11-01 00:15", "M2", "0")  # 非末次实读
        with self.assertRaises(storage.MeterChangeError):
            storage.register_meter_change(
                self.conn, "2026-11-01 00:00", "M2", "0")  # 不晚于首读
        # 正确登记
        storage.register_meter_change(
            self.conn, "2026-11-01 00:30", "M2", "5")
        with self.assertRaises(storage.MeterChangeError):
            storage.register_meter_change(
                self.conn, "2026-11-01 00:30", "M3", "5")  # 新表仅首读
        # 新表补一点后可再次换表（同一期多次）
        self._upload(
            "timestamp,reading\n2026-11-01 00:45,20\n", meter_seq=1)
        rec2 = storage.register_meter_change(
            self.conn, "2026-11-01 00:45", "M3", "0")
        # 偏移 55 + 20 = 统一接续值 75
        self.assertEqual(rec2["unified_value"], 75)
        meters = [m["meter_id"] for m in storage.list_meters(self.conn)]
        self.assertEqual(meters, ["M1", "M2", "M3"])

    def test_duplicate_meter_id_and_bad_start_rejected(self):
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n")
        with self.assertRaises(storage.MeterChangeError):
            storage.register_meter_change(
                self.conn, "2026-11-01 00:15", "M1", "0")
        with self.assertRaises(storage.MeterChangeError):
            storage.register_meter_change(
                self.conn, "2026-11-01 00:15", "M2", "-3")
        with self.assertRaises(storage.UploadError):
            storage.register_meter_change(
                self.conn, "not-a-time", "M2", "0")

    def test_span_bounds_and_other_segment_correction_blocked(self):
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n2026-11-01 00:30,60\n")
        storage.register_meter_change(
            self.conn, "2026-11-01 00:30", "M2", "10")
        self._upload(
            "timestamp,reading\n2026-11-01 00:45,40\n", meter_seq=1)
        spans = storage.meter_spans(self.conn)
        self.assertEqual(spans[0],
                         (None, datetime(2026, 11, 1, 0, 30)))
        self.assertEqual(spans[1],
                         (datetime(2026, 11, 1, 0, 30), None))
        # 新表不得写交接分钟之前的点（其他时段的非交接读数）
        with self.assertRaises(storage.UploadError):
            self._upload(
                "timestamp,reading\n2026-11-01 00:15,1\n", meter_seq=1)
        # 旧表不得写交接分钟之后的点
        with self.assertRaises(storage.UploadError):
            self._upload(
                "timestamp,reading\n2026-11-01 00:45,95\n", meter_seq=0)
        # 两侧交接分钟均可分别更正
        self._upload(
            "timestamp,reading\n2026-11-01 00:30,66\n", meter_seq=0)
        self._upload(
            "timestamp,reading\n2026-11-01 00:30,16\n", meter_seq=1)
        info = storage.lookup_minute(
            self.conn, datetime(2026, 11, 1, 0, 30))
        self.assertEqual(info["continuation_value"], 66)
        chg = storage.list_changes(self.conn)[0]
        self.assertEqual((chg["old_last_reading"],
                          chg["new_start_reading"],
                          chg["unified_value"]), (66, 16, 66))

    def test_correction_recomputes_offsets_and_rollback_rejects_batch(self):
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:30,60\n")
        storage.register_meter_change(
            self.conn, "2026-11-01 00:30", "M2", "10")  # 偏移 50
        self._upload(
            "timestamp,reading\n2026-11-01 01:00,70\n", meter_seq=1)
        storage.register_meter_change(
            self.conn, "2026-11-01 01:00", "M3", "0")   # 统一 120
        # 把旧表 00:30 末读改小到 40：M2 偏移变 30，M3 统一值变 100
        self._upload(
            "timestamp,reading\n2026-11-01 00:30,40\n", meter_seq=0)
        series = dict(storage.load_readings(self.conn))
        self.assertEqual(series[datetime(2026, 11, 1, 0, 30)], 40)
        self.assertEqual(series[datetime(2026, 11, 1, 1, 0)], 100)
        info = storage.lookup_minute(
            self.conn, datetime(2026, 11, 1, 1, 0))
        self.assertEqual(info["continuation_value"], 100)
        # 回卷批次：M2 的 01:00 从 70 改成 5（小于其 00:30 起始 10）
        bad = storage.parse_csv(_csv(
            "timestamp,reading\n2026-11-01 01:00,5\n"))
        with self.assertRaises(storage.UploadError) as ctx:
            storage.merge_upload(self.conn, bad, 1)
        self.assertIn("回卷", str(ctx.exception))
        # 整批拒绝，接续值照旧
        self.assertEqual(dict(storage.load_readings(self.conn)), series)

    def test_cap_violation_after_merge_rejected(self):
        storage.save_config(self.conn, 5, None, None)
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n2026-11-01 00:30,60\n")
        # 换表：接续序列不变，仍可行
        storage.register_meter_change(
            self.conn, "2026-11-01 00:30", "M2", "0")
        # 新表 15 分钟用 100 Wh，超上限 75 → 整批拒绝，接续值照旧
        with self.assertRaises(storage.UploadError):
            self._upload(
                "timestamp,reading\n2026-11-01 00:45,100\n", meter_seq=1)
        minutes = {r[0] for r in storage.load_raw_readings(self.conn)}
        self.assertNotIn(datetime(2026, 11, 1, 0, 45), minutes)

    def test_change_that_breaks_cap_rejected_and_state_kept(self):
        storage.save_config(self.conn, 2, None, None)
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n")  # 30 = 15*2 恰好上限
        # 新表起始 50，偏移 -20；接续序列不变，仍可行 → 允许
        storage.register_meter_change(
            self.conn, "2026-11-01 00:15", "M2", "50")
        n_meters = len(storage.list_meters(self.conn))
        # 新表 00:30 读数 100（折算 80），15 分钟 +50 超 30 上限 → 拒绝
        with self.assertRaises(storage.UploadError):
            self._upload(
                "timestamp,reading\n2026-11-01 00:30,100\n", meter_seq=1)
        self.assertEqual(len(storage.list_meters(self.conn)), n_meters)

    def test_suspend_on_change_and_correction_resume_on_reaudit(self):
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n2026-11-01 00:30,60\n")
        service.run_audit(self.conn, "10", "30", "2026-11-01 00:00")
        self.assertFalse(
            storage.latest_audit(self.conn, "current")["suspended"])
        # 换表 → 当前结论暂停，初核保留
        storage.register_meter_change(
            self.conn, "2026-11-01 00:30", "M2", "9")
        self.assertTrue(
            storage.latest_audit(self.conn, "current")["suspended"])
        self.assertFalse(
            storage.latest_audit(self.conn, "initial")["suspended"])
        # 交接读数更正 → 仍暂停
        self._upload(
            "timestamp,reading\n2026-11-01 00:30,69\n", meter_seq=0)
        self.assertTrue(
            storage.latest_audit(self.conn, "current")["suspended"])
        # 用接续值重新核对 → 恢复（窗 0、窗1 固定 30/39，均不超过 39）
        r = service.run_audit(
            self.conn, "10", "39", "2026-11-01 00:15")
        self.assertEqual(r.status, "feasible")
        self.assertFalse(
            storage.latest_audit(self.conn, "current")["suspended"])

    def test_audit_uses_continuation_values(self):
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n2026-11-01 00:30,60\n")
        # 新表从 1000 起，偏移 -940：接续序列与单表语义一致
        storage.register_meter_change(
            self.conn, "2026-11-01 00:30", "M2", "1000")
        self._upload(
            "timestamp,reading\n2026-11-01 00:45,1090\n", meter_seq=1)
        # 接续总量 150 Wh；各窗固定 30/30/90，报窗 2=90 可行
        r = service.run_audit(
            self.conn, "10", "90", "2026-11-01 00:30")
        self.assertEqual(r.status, "feasible")
        self.assertEqual(sum(r.window_totals), 150)

    def test_lookup_read_budget_under_32_for_changes_and_summary(self):
        # 逐块表推进：每块表两个点，在末次实读分钟换表，共 10 次交接
        from datetime import timedelta
        base = datetime(2026, 11, 1, 0, 0)
        self._upload(
            "timestamp,reading\n"
            f"{base:%Y-%m-%d %H:%M},0\n"
            f"{base + timedelta(minutes=15):%Y-%m-%d %H:%M},30\n")
        for i in range(1, 11):
            h = base + timedelta(minutes=15 * i)
            storage.register_meter_change(
                self.conn, f"{h:%Y-%m-%d %H:%M}", f"M{i + 1}", str(30 * i))
            if i < 10:
                nxt = base + timedelta(minutes=15 * (i + 1))
                self._upload(
                    "timestamp,reading\n"
                    f"{nxt:%Y-%m-%d %H:%M},{30 * (i + 1)}\n", meter_seq=i)
        changes = storage.list_changes(self.conn)
        summary = storage.get_summary(self.conn)
        # 需求口径：换表记录 + 接续汇总合计 ≤ 32
        self.assertEqual(len(changes), 10)
        self.assertLessEqual(len(changes) + 1, 32)
        self.assertEqual(summary["handover_count"], 10)

        # 分钟查询走索引而非全表扫描
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM readings WHERE minute_ts=?",
            ((base + timedelta(minutes=30)).strftime(storage.FMT),)
        ).fetchall()
        self.assertTrue(any("USING INDEX" in p["detail"] for p in plan))
        info = storage.lookup_minute(self.conn, base + timedelta(minutes=30))
        self.assertEqual(len(info["raw_readings"]), 2)  # 交接分钟两侧
        self.assertEqual(info["continuation_value"], 60)
        # 非交接、无实读的分钟
        info2 = storage.lookup_minute(self.conn, base + timedelta(minutes=7))
        self.assertEqual(info2["raw_readings"], [])
        # 无实读的分钟也安全返回
        info3 = storage.lookup_minute(
            self.conn, datetime(2026, 11, 1, 3, 0))
        self.assertEqual(info3["raw_readings"], [])
        self.assertIsNone(info3["continuation_value"])

    def test_reset_clears_changes_and_summary(self):
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n")
        storage.register_meter_change(
            self.conn, "2026-11-01 00:15", "M2", "3")
        service.run_audit(self.conn, "10", "30", "2026-11-01 00:00")
        storage.reset_all(self.conn)
        self.assertEqual(storage.list_changes(self.conn), [])
        self.assertEqual(storage.load_raw_readings(self.conn), [])
        self.assertEqual(storage.load_readings(self.conn), [])
        self.assertIsNone(storage.latest_audit(self.conn, "current"))
        self.assertEqual(
            storage.get_summary(self.conn)["handover_count"], 0)
        meters = [m["meter_id"] for m in storage.list_meters(self.conn)]
        self.assertEqual(meters, ["M1"])
        # 清空后可重新开始
        self._upload(
            "timestamp,reading\n2026-11-01 00:00,0\n"
            "2026-11-01 00:15,30\n")
        self.assertEqual(len(storage.load_readings(self.conn)), 2)


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "web2.db")
        self.app = create_app(self.db)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_http_change_lookup_suspend_and_csv(self):
        data = {"csv": (io.BytesIO(
            b"timestamp,reading\n2026-11-01 00:00,0\n"
            b"2026-11-01 00:15,30\n2026-11-01 00:30,60\n"),
            "r.csv"), "meter_seq": "0"}
        self.assertEqual(self.client.post(
            "/upload", data=data,
            content_type="multipart/form-data").status_code, 302)
        # 核对一次
        self.client.post("/audit", data={
            "per_minute_cap": "10", "peak_wh": "30",
            "reported_window": "2026-11-01 00:00"})
        # 换表
        resp = self.client.post("/change", data={
            "handover_minute": "2026-11-01 00:30",
            "new_meter_id": "M2", "new_start_reading": "6"})
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").data.decode()
        self.assertIn("已暂停", page)
        # 暂停期间 CSV 不提供
        resp = self.client.get("/windows.csv")
        self.assertEqual(resp.status_code, 302)
        # 按分钟 JSON
        resp = self.client.get(
            "/minute?format=json&minute=2026-11-01 00:30")
        payload = resp.get_json()
        self.assertEqual(payload["continuation_value"], 60)
        self.assertEqual(len(payload["raw_readings"]), 2)
        # 新表补传
        data = {"csv": (io.BytesIO(
            b"timestamp,reading\n2026-11-01 00:45,36\n"), "r2.csv"),
            "meter_seq": "1"}
        self.assertEqual(self.client.post(
            "/upload", data=data,
            content_type="multipart/form-data").status_code, 302)
        # 重新核对恢复（各窗均 30 Wh）
        self.client.post("/audit", data={
            "per_minute_cap": "10", "peak_wh": "30",
            "reported_window": "2026-11-01 00:00"})
        resp = self.client.get("/windows.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("window_start,window_index", resp.data.decode())
        self.assertNotIn("已暂停提供",
                         self.client.get("/").data.decode())
        # 越界使用时段被整批拒绝
        data = {"csv": (io.BytesIO(
            b"timestamp,reading\n2026-11-01 00:15,1\n"), "bad.csv"),
            "meter_seq": "1"}
        self.client.post("/upload", data=data,
                         content_type="multipart/form-data")
        self.assertIn("整批拒绝", self.client.get("/").data.decode())

    def test_http_bad_change_flash(self):
        data = {"csv": (io.BytesIO(
            b"timestamp,reading\n2026-11-01 00:00,0\n"
            b"2026-11-01 00:15,30\n"), "r.csv"), "meter_seq": "0"}
        self.client.post("/upload", data=data,
                         content_type="multipart/form-data")
        self.client.post("/change", data={
            "handover_minute": "2026-11-01 00:00",  # 不晚于首读
            "new_meter_id": "M2", "new_start_reading": "0"})
        self.assertIn("换表登记被拒绝",
                      self.client.get("/").data.decode())

    def test_reset_via_http_clears_change_records(self):
        data = {"csv": (io.BytesIO(
            b"timestamp,reading\n2026-11-01 00:00,0\n"
            b"2026-11-01 00:15,30\n"), "r.csv"), "meter_seq": "0"}
        self.client.post("/upload", data=data,
                         content_type="multipart/form-data")
        self.client.post("/change", data={
            "handover_minute": "2026-11-01 00:15",
            "new_meter_id": "M2", "new_start_reading": "3"})
        self.client.post("/reset")
        page = self.client.get("/").data.decode()
        self.assertIn("M1（首表", page)
        # 结构上只剩首表一个选项，且无换表记录表
        self.assertEqual(page.count('<option value='), 1)
        self.assertNotIn("换表记录与接续折算", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
