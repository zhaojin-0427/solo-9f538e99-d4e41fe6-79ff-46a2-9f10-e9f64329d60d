"""SQLite 存储层：一块（或多块交接的）电表的一期数据。

表：
- config               单行：核账参数（每分钟上限、峰值、所报窗起点）
- meters               本期使用过的电表登记：seq=0 为首表（旧库读数归首表）
- meter_changes        换表交接记录：交接分钟、旧/新表、两侧读数、折算接续值
- continuation_summary 接续汇总（单行）：首表编号、首表原示数、交接次数
- readings             每分钟原始实读，按 (分钟, 电表) 唯一；同刻同表覆盖
- audits               核账结论：initial 只写一次；current 每次更新，
                       换表/交接读数更正后 suspended=1，重新核对后恢复

行数约束：meter_changes 最多 31 条（31 天账期每天至多一次），
continuation_summary 固定 1 行，二者合计最多 32 条。
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import solver

FMT = "%Y-%m-%d %H:%M"
FIRST_METER_ID = "M1"
MAX_HANDOVERS = 31          # 加上接续汇总单行，换表记录+汇总合计 ≤ 32 条

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    per_minute_cap INTEGER,
    peak_wh INTEGER,
    reported_window TEXT
);
CREATE TABLE IF NOT EXISTS meters (
    seq INTEGER PRIMARY KEY,
    meter_id TEXT NOT NULL UNIQUE,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meter_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_minute TEXT NOT NULL,
    old_meter_seq INTEGER NOT NULL,
    new_meter_seq INTEGER NOT NULL,
    old_last_reading INTEGER NOT NULL,
    new_start_reading INTEGER NOT NULL,
    unified_value INTEGER NOT NULL,
    created_ts TEXT NOT NULL,
    UNIQUE(handover_minute, new_meter_seq)
);
CREATE INDEX IF NOT EXISTS idx_changes_handover
    ON meter_changes(handover_minute);
CREATE TABLE IF NOT EXISTS continuation_summary (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    first_meter_id TEXT,
    first_reading INTEGER,
    handover_count INTEGER NOT NULL DEFAULT 0,
    updated_ts TEXT
);
CREATE TABLE IF NOT EXISTS readings (
    minute_ts TEXT NOT NULL,
    meter_seq INTEGER NOT NULL DEFAULT 0,
    reading_wh INTEGER NOT NULL,
    PRIMARY KEY (minute_ts, meter_seq)
);
CREATE INDEX IF NOT EXISTS idx_readings_minute ON readings(minute_ts);
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('initial', 'current')),
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    suspended INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audits_kind ON audits(kind);
"""

# 旧版 readings（无 meter_seq）/ audits（无 suspended）的迁移脚本
_READINGS_MIGRATION = """
CREATE TABLE readings_new (
    minute_ts TEXT NOT NULL,
    meter_seq INTEGER NOT NULL DEFAULT 0,
    reading_wh INTEGER NOT NULL,
    PRIMARY KEY (minute_ts, meter_seq)
);
INSERT INTO readings_new(minute_ts, meter_seq, reading_wh)
    SELECT minute_ts, 0, reading_wh FROM readings;
DROP TABLE readings;
ALTER TABLE readings_new RENAME TO readings;
CREATE INDEX idx_readings_minute ON readings(minute_ts);
"""

TS_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
              "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


class UploadError(ValueError):
    """CSV / 补传整批拒绝。"""


class MeterChangeError(UploadError):
    """换表登记被拒绝（库内状态不变）。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # 旧库结构迁移
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(readings)")}
    if "meter_seq" not in cols:
        conn.executescript(_READINGS_MIGRATION)
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(audits)")}
    if "suspended" not in acols:
        conn.execute(
            "ALTER TABLE audits ADD COLUMN suspended INTEGER NOT NULL DEFAULT 0")
    if conn.execute("SELECT COUNT(*) FROM config").fetchone()[0] == 0:
        conn.execute("INSERT INTO config(id) VALUES(1)")
    if conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO meters(seq, meter_id, created_ts) VALUES(0, ?, ?)",
            (FIRST_METER_ID, _now()))
    if conn.execute(
            "SELECT COUNT(*) FROM continuation_summary").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO continuation_summary(id, handover_count) "
            "VALUES(1, 0)")
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #
def parse_csv(content: bytes) -> Dict[datetime, int]:
    """解析整分钟累计读数 CSV。同文件内同刻取最后一条。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadError(f"CSV 必须为 UTF-8 编码：{exc}") from exc
    reader = csv.reader(io.StringIO(text))
    rows: List[List[str]] = [r for r in reader if r and any(c.strip() for c in r)]
    if not rows:
        raise UploadError("CSV 为空")

    header = [c.strip().lower() for c in rows[0]]
    data_rows = rows[1:]
    if header[:2] not in (["timestamp", "reading"],
                          ["time", "reading"],
                          ["minute_ts", "reading_wh"]):
        # 无表头容错：首行也当数据
        if _parse_int(rows[0][1]) is None:
            raise UploadError("CSV 表头应为 timestamp,reading")
        data_rows = rows

    parsed: Dict[datetime, int] = {}
    for lineno, row in enumerate(data_rows, start=2):
        if len(row) < 2:
            raise UploadError(f"第 {lineno} 行列数不足，应为 timestamp,reading")
        ts = _parse_ts(row[0].strip(), lineno)
        val = _parse_int(row[1].strip())
        if val is None or val < 0:
            raise UploadError(f"第 {lineno} 行读数必须是非负整数 Wh")
        parsed[ts] = val  # 同刻覆盖
    if not parsed:
        raise UploadError("CSV 没有有效数据行")
    return parsed


def _parse_int(s: str) -> Optional[int]:
    try:
        if s.strip() != str(int(s.strip())):
            return None
        return int(s.strip())
    except (ValueError, TypeError):
        return None


def _parse_ts(s: str, label: str = "时间") -> datetime:
    for fmt in TS_FORMATS:
        try:
            dt = datetime.strptime(s.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        raise UploadError(
            f"{label} {s!r} 无法解析，格式示例 2026-11-01 08:00")
    if dt.second != 0 or dt.microsecond != 0:
        raise UploadError(f"{label}必须是整分钟")
    return dt


# --------------------------------------------------------------------------- #
# 电表 / 换表 / 汇总 查询
# --------------------------------------------------------------------------- #
def list_meters(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meters ORDER BY seq").fetchall()


def list_changes(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meter_changes ORDER BY seq").fetchall()


def get_summary(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM continuation_summary WHERE id=1").fetchone()


def meter_spans(conn: sqlite3.Connection
                ) -> Dict[int, Tuple[Optional[datetime], Optional[datetime]]]:
    """各电表使用时段：(起始分钟, 截止分钟)，均含交接分钟；末表截止为 None。"""
    starts: Dict[int, datetime] = {}
    ends: Dict[int, datetime] = {}
    for c in list_changes(conn):
        h = datetime.strptime(c["handover_minute"], FMT)
        starts[c["new_meter_seq"]] = h
        ends[c["old_meter_seq"]] = h
    return {m["seq"]: (starts.get(m["seq"]), ends.get(m["seq"]))
            for m in list_meters(conn)}


def _load_raw_by_meter(conn: sqlite3.Connection
                       ) -> Dict[int, List[Tuple[datetime, int]]]:
    snap: Dict[int, List[Tuple[datetime, int]]] = {}
    for r in conn.execute(
            "SELECT minute_ts, meter_seq, reading_wh FROM readings "
            "ORDER BY minute_ts"):
        ts = datetime.strptime(r["minute_ts"], FMT)
        snap.setdefault(r["meter_seq"], []).append((ts, r["reading_wh"]))
    return snap


def load_raw_readings(conn: sqlite3.Connection
                      ) -> List[Tuple[datetime, int, str, int]]:
    """全部分钟原始实读：(分钟, 电表seq, 电表编号, 读数)。"""
    return [(datetime.strptime(r["minute_ts"], FMT),
             r["meter_seq"], r["meter_id"], r["reading_wh"])
            for r in conn.execute(
                "SELECT r.minute_ts, r.meter_seq, m.meter_id, r.reading_wh "
                "FROM readings r JOIN meters m ON m.seq = r.meter_seq "
                "ORDER BY r.minute_ts, r.meter_seq")]


# --------------------------------------------------------------------------- #
# 接续值折算
# --------------------------------------------------------------------------- #
def offsets_from_records(changes: List[dict]) -> Dict[int, int]:
    """以换表记录为接续真值，链式推导各电表偏移。

    每条记录固化了交接分钟的统一接续值 unified_value 与新表起始读数，
    新表偏移 = unified_value - new_start_reading；首表偏移 0。
    """
    offsets: Dict[int, int] = {0: 0}
    for c in changes:
        offsets[c["new_meter_seq"]] = (
            c["unified_value"] - c["new_start_reading"])
    return offsets


def _recompute(snap: Dict[int, List[Tuple[datetime, int]]],
               changes: List[dict]
               ) -> Tuple[Dict[int, int], List[dict]]:
    """按交接顺序重算各表偏移与交接分钟统一接续值。

    累计值从首表原示数起算：首表偏移 0；每次交接把旧表末读折算成统一值，
    新表偏移 = 统一值 - 新表起始读数。两侧交接分钟含同一分钟，折算后相等。
    """
    offsets: Dict[int, int] = {0: 0}
    for seq in snap:
        offsets.setdefault(seq, 0)
    updated: List[dict] = []
    for c in changes:
        h = datetime.strptime(c["handover_minute"], FMT)
        old_rows = dict(snap.get(c["old_meter_seq"], []))
        new_rows = dict(snap.get(c["new_meter_seq"], []))
        if h not in old_rows or h not in new_rows:
            raise UploadError(
                f"交接分钟 {c['handover_minute']} 缺少旧表末读或新表起始读数")
        old_v, new_v = old_rows[h], new_rows[h]
        unified = offsets[c["old_meter_seq"]] + old_v
        offsets[c["new_meter_seq"]] = unified - new_v
        rec = dict(c)
        rec["old_last_reading"] = old_v
        rec["new_start_reading"] = new_v
        rec["unified_value"] = unified
        updated.append(rec)
    return offsets, updated


def _continuation_series(snap: Dict[int, List[Tuple[datetime, int]]],
                         offsets: Dict[int, int]
                         ) -> List[Tuple[datetime, int]]:
    points: List[Tuple[datetime, int]] = []
    for seq, rows in snap.items():
        off = offsets.get(seq, 0)
        points.extend((ts, v + off) for ts, v in rows)
    points.sort()
    dedup: List[Tuple[datetime, int]] = []
    for ts, val in points:
        if dedup and dedup[-1][0] == ts:
            if dedup[-1][1] != val:
                raise UploadError(
                    f"交接分钟 {ts:{FMT}} 两侧折算接续值不一致，整批拒绝")
            continue  # 两侧均含该分钟，只保留一个点
        dedup.append((ts, val))
    return dedup


def _check_handover_consistency(
        snap: Dict[int, List[Tuple[datetime, int]]],
        changes: List[dict], offsets: Dict[int, int]) -> None:
    """校验换表记录与原始实读在交接分钟折算一致（发现脱钩即拒）。"""
    raw_by_meter = {seq: dict(rows) for seq, rows in snap.items()}
    for c in changes:
        h = datetime.strptime(c["handover_minute"], FMT)
        old_rows = raw_by_meter.get(c["old_meter_seq"], {})
        new_rows = raw_by_meter.get(c["new_meter_seq"], {})
        if h not in old_rows or h not in new_rows:
            raise UploadError(
                f"交接分钟 {c['handover_minute']} 缺少旧表末读或新表起始读数")
        old_unified = offsets[c["old_meter_seq"]] + old_rows[h]
        new_unified = offsets[c["new_meter_seq"]] + new_rows[h]
        if old_unified != c["unified_value"] or \
                new_unified != c["unified_value"] or \
                old_unified != new_unified:
            raise UploadError(
                f"交接分钟 {c['handover_minute']} 两侧交接读数折算不成同一值，"
                "整批拒绝")


def _validate(snap: Dict[int, List[Tuple[datetime, int]]],
              offsets: Dict[int, int], cap: Optional[int]) -> None:
    """整批校验：读数非负、各表不回卷、合并接续不回卷、账期结构、每分钟上限。"""
    for seq, rows in snap.items():
        prev_v = None
        prev_t = None
        for ts, v in rows:
            if v < 0:
                raise UploadError(f"电表 seq={seq} 读数必须是非负整数")
            if prev_v is not None and v < prev_v:
                raise UploadError(
                    "电表不回卷，发现冲突的相邻读数："
                    f"{prev_t:{FMT}} = {prev_v} Wh -> {ts:{FMT}} = {v} Wh，"
                    "整批拒绝")
            prev_t, prev_v = ts, v

    series = _continuation_series(snap, offsets)
    if not series:
        return
    if len(series) < 2:
        raise UploadError("至少需要两个读数才能确定账期")
    first_ts, last_ts = series[0][0], series[-1][0]
    if first_ts.minute % solver.WINDOW_MINUTES or \
            last_ts.minute % solver.WINDOW_MINUTES:
        raise UploadError(
            "账期两端必须对齐 15 分钟边界："
            f"首读数 {first_ts:{FMT}}、末读数 {last_ts:{FMT}}，整批拒绝")
    ordered = [(int((ts - first_ts).total_seconds()) // 60, v)
               for ts, v in series]
    try:
        solver.validate_period(ordered)
        if cap:
            solver.build_pieces(
                ordered, cap,
                (ordered[-1][0] - ordered[0][0])
                // solver.WINDOW_MINUTES)
    except solver.SolverError as exc:
        raise UploadError(f"合并后实读无解：{exc}，整批拒绝") from exc


def _refresh_summary(conn: sqlite3.Connection,
                     snap: Dict[int, List[Tuple[datetime, int]]]) -> None:
    rows0 = snap.get(0, [])
    first_reading = rows0[0][1] if rows0 else None
    handover_count = conn.execute(
        "SELECT COUNT(*) FROM meter_changes").fetchone()[0]
    conn.execute(
        "UPDATE continuation_summary SET first_meter_id=?, first_reading=?, "
        "handover_count=?, updated_ts=? WHERE id=1",
        (FIRST_METER_ID, first_reading, handover_count, _now()))


def _suspend_current(conn: sqlite3.Connection) -> None:
    """换表或交接读数更正成功后，暂停旧的当前结论（初核保留）。"""
    row = conn.execute(
        "SELECT id FROM audits WHERE kind='current' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        conn.execute(
            "UPDATE audits SET suspended=1 WHERE id=?", (row["id"],))


# --------------------------------------------------------------------------- #
# 补传合并（选定电表使用时段）
# --------------------------------------------------------------------------- #
def merge_upload(conn: sqlite3.Connection,
                 incoming: Dict[datetime, int],
                 meter_seq: int = 0) -> Tuple[int, int]:
    """向指定电表的使用时段合并一批原始读数，全部校验通过才写库。

    时段以交接分钟为界、两侧均含该分钟；同一 (分钟, 电表) 同刻覆盖。
    交接分钟读数被更正时，重算全部接续值并暂停当前结论。
    返回 (合并后原始读数总条数, 本批覆盖条数)。
    """
    meter_seqs = {m["seq"] for m in list_meters(conn)}
    if meter_seq not in meter_seqs:
        raise UploadError("所选电表不存在，请刷新页面后重试")
    start, end = meter_spans(conn).get(meter_seq, (None, None))
    for ts in incoming:
        if start is not None and ts < start:
            raise UploadError(
                f"{ts:{FMT}} 早于该电表使用时段起点 {start:{FMT}}，"
                "其他使用时段只可读对应的交接读数，整批拒绝")
        if end is not None and ts > end:
            raise UploadError(
                f"{ts:{FMT}} 晚于该电表使用时段截止 {end:{FMT}}，"
                "其他使用时段只可读对应的交接读数，整批拒绝")

    snap = _load_raw_by_meter(conn)
    existing = {ts for ts, _ in snap.get(meter_seq, [])}
    overwritten = sum(1 for ts in incoming if ts in existing)
    merged = dict(snap.get(meter_seq, []))
    merged.update(incoming)  # 同一时段同刻覆盖
    snap[meter_seq] = sorted(merged.items())

    changes = [dict(r) for r in list_changes(conn)]
    offsets, updated_changes = _recompute(snap, changes)
    cap = get_config(conn)["per_minute_cap"]
    _validate(snap, offsets, cap)  # 失败直接抛错，库内数据与接续值照旧

    handover_minutes = {c["handover_minute"] for c in changes}
    touched_handover = any(
        ts.strftime(FMT) in handover_minutes for ts in incoming)

    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM readings WHERE meter_seq=?", (meter_seq,))
        conn.executemany(
            "INSERT INTO readings(minute_ts, meter_seq, reading_wh) "
            "VALUES(?, ?, ?)",
            [(ts.strftime(FMT), meter_seq, v)
             for ts, v in snap[meter_seq]])
        for c in updated_changes:
            conn.execute(
                "UPDATE meter_changes SET old_last_reading=?, "
                "new_start_reading=?, unified_value=? WHERE seq=?",
                (c["old_last_reading"], c["new_start_reading"],
                 c["unified_value"], c["seq"]))
        _refresh_summary(conn, snap)
        if touched_handover:
            _suspend_current(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    total = sum(len(v) for v in snap.values())
    return total, overwritten


# --------------------------------------------------------------------------- #
# 换表登记
# --------------------------------------------------------------------------- #
def register_meter_change(conn: sqlite3.Connection,
                          handover_text: str,
                          new_meter_id: str,
                          start_text: str) -> sqlite3.Row:
    """在现用表末次实读的分钟登记新表编号与起始读数。

    交接分钟必须等于现用表末次实读分钟、且晚于现用表首读；同一期可多次换表。
    校验失败抛 MeterChangeError，库内状态不变；成功后暂停当前结论。
    """
    h = _parse_ts(handover_text, "交接分钟")
    start = _parse_int(start_text)
    if start is None or start < 0:
        raise MeterChangeError("新表起始读数必须是非负整数 Wh")
    new_meter_id = (new_meter_id or "").strip()
    if not new_meter_id:
        raise MeterChangeError("请填写新电表编号")

    meters = list_meters(conn)
    changes = list_changes(conn)
    if len(changes) >= MAX_HANDOVERS:
        raise MeterChangeError(
            f"同一账期最多换表 {MAX_HANDOVERS} 次（换表记录与接续汇总"
            "合计不超过 32 条）")
    if any(m["meter_id"] == new_meter_id for m in meters):
        raise MeterChangeError(f"电表编号 {new_meter_id} 已存在")

    active_seq = meters[-1]["seq"]
    snap = _load_raw_by_meter(conn)
    active = snap.get(active_seq, [])
    if not active:
        raise MeterChangeError("现用表尚无实读，无法登记换表")
    first_min, last_min = active[0][0], active[-1][0]
    if h != last_min:
        raise MeterChangeError(
            f"交接分钟必须是现用表末次实读的分钟（{last_min:{FMT}}），"
            f"收到 {h:{FMT}}")
    if h <= first_min:
        raise MeterChangeError(
            f"交接须晚于现用表首读（{first_min:{FMT}}）")

    new_seq = active_seq + 1
    trial_snap = {k: list(v) for k, v in snap.items()}
    trial_snap[new_seq] = [(h, start)]
    trial_change = {
        "seq": None,
        "handover_minute": h.strftime(FMT),
        "old_meter_seq": active_seq,
        "new_meter_seq": new_seq,
        "old_last_reading": None,
        "new_start_reading": start,
        "unified_value": None,
    }
    trial_changes = [dict(r) for r in changes] + [trial_change]
    offsets, updated = _recompute(trial_snap, trial_changes)
    cap = get_config(conn)["per_minute_cap"]
    _validate(trial_snap, offsets, cap)  # 越界/回卷/超上限整批拒绝
    new_record = updated[-1]

    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO meters(seq, meter_id, created_ts) VALUES(?, ?, ?)",
            (new_seq, new_meter_id, _now()))
        cur = conn.execute(
            "INSERT INTO meter_changes(handover_minute, old_meter_seq, "
            "new_meter_seq, old_last_reading, new_start_reading, "
            "unified_value, created_ts) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (new_record["handover_minute"], active_seq, new_seq,
             new_record["old_last_reading"], new_record["new_start_reading"],
             new_record["unified_value"], _now()))
        conn.execute(
            "INSERT INTO readings(minute_ts, meter_seq, reading_wh) "
            "VALUES(?, ?, ?)",
            (h.strftime(FMT), new_seq, start))
        _refresh_summary(conn, trial_snap)
        _suspend_current(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute(
        "SELECT * FROM meter_changes WHERE seq=?", (cur.lastrowid,)).fetchone()


# --------------------------------------------------------------------------- #
# 按分钟查询：只读该分钟实读；换表记录+汇总合计 ≤ 32 条
# --------------------------------------------------------------------------- #
def lookup_minute(conn: sqlite3.Connection, ts: datetime) -> dict:
    """返回该分钟的原始实读、换表记录与接续值。

    读数只查该分钟（交接分钟最多两块表各一条）；换表记录最多 31 条，
    接续偏移直接由记录上的 unified_value/new_start_reading 链式推出。
    """
    key = ts.strftime(FMT)
    raw = conn.execute(
        "SELECT r.meter_seq, m.meter_id, r.reading_wh "
        "FROM readings r JOIN meters m ON m.seq = r.meter_seq "
        "WHERE r.minute_ts=? ORDER BY r.meter_seq", (key,)).fetchall()
    change = conn.execute(
        "SELECT * FROM meter_changes WHERE handover_minute=?",
        (key,)).fetchone()
    # 截至该分钟的换表记录（≤31 条），以记录为真值推算各表偏移
    prior_changes = conn.execute(
        "SELECT * FROM meter_changes WHERE handover_minute <= ? "
        "ORDER BY seq", (key,)).fetchall()
    offsets = offsets_from_records([dict(c) for c in prior_changes])

    continuation = None
    if raw:
        vals = {r["meter_seq"]:
                r["reading_wh"] + offsets.get(r["meter_seq"], 0)
                for r in raw}
        continuation = next(iter(vals.values()))
        if len(set(vals.values())) > 1:
            raise UploadError(
                f"交接分钟 {key} 两侧折算接续值不一致")
        # 交接分钟：原始实读折算须与换表记录固化的统一值一致
        if change is not None and \
                continuation != change["unified_value"]:
            raise UploadError(
                f"交接分钟 {key} 交接读数折算值 {continuation} 与换表记录 "
                f"统一接续值 {change['unified_value']} 不一致，整批拒绝")
    return {
        "minute": key,
        "raw_readings": [dict(r) for r in raw],
        "meter_change": dict(change) if change else None,
        "continuation_value": continuation,
    }


# --------------------------------------------------------------------------- #
# 接续后的完整读数序列（供 solver 使用）
# --------------------------------------------------------------------------- #
def load_readings(conn: sqlite3.Connection
                  ) -> List[Tuple[datetime, int]]:
    """合并折算后的接续序列：交接分钟两侧去重，严格按分钟递增。

    偏移以换表记录（接续真值）为准，并交叉校验原始实读在交接分钟折算一致。
    """
    snap = _load_raw_by_meter(conn)
    changes = [dict(r) for r in list_changes(conn)]
    offsets = offsets_from_records(changes)
    if changes:
        _check_handover_consistency(snap, changes, offsets)
    return _continuation_series(snap, offsets)


def _to_offsets(ordered: List[Tuple[datetime, int]]
                ) -> List[Tuple[int, int]]:
    t0 = ordered[0][0]
    return [(int((ts - t0).total_seconds()) // 60, v)
            for ts, v in ordered]


# --------------------------------------------------------------------------- #
# 配置与结论
# --------------------------------------------------------------------------- #
def save_config(conn: sqlite3.Connection, cap: Optional[int],
                peak: Optional[int], window_ts: Optional[str]) -> None:
    with conn:
        conn.execute(
            "UPDATE config SET per_minute_cap=?, peak_wh=?, "
            "reported_window=? WHERE id=1", (cap, peak, window_ts))


def get_config(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM config WHERE id=1").fetchone()


def latest_audit(conn: sqlite3.Connection, kind: str
                 ) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audits WHERE kind=? ORDER BY id DESC LIMIT 1",
        (kind,)).fetchone()


def has_initial(conn: sqlite3.Connection) -> bool:
    return latest_audit(conn, "initial") is not None


def add_audit(conn: sqlite3.Connection, kind: str, status: str,
              message: str, detail_json: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audits(kind, status, message, detail_json, "
            "created_ts, suspended) VALUES(?, ?, ?, ?, ?, 0)",
            (kind, status, message, detail_json, _now()))


def reset_all(conn: sqlite3.Connection) -> None:
    """清空账期：读数、结论、换表记录、电表登记、接续汇总、参数一并清空。"""
    with conn:
        conn.execute("DELETE FROM readings")
        conn.execute("DELETE FROM audits")
        conn.execute("DELETE FROM meter_changes")
        conn.execute("DELETE FROM meters")
        conn.execute(
            "INSERT INTO meters(seq, meter_id, created_ts) VALUES(0, ?, ?)",
            (FIRST_METER_ID, _now()))
        conn.execute(
            "UPDATE continuation_summary SET first_meter_id=?, "
            "first_reading=NULL, handover_count=0, updated_ts=? WHERE id=1",
            (FIRST_METER_ID, _now()))
        conn.execute("UPDATE config SET per_minute_cap=NULL, "
                     "peak_wh=NULL, reported_window=NULL WHERE id=1")
