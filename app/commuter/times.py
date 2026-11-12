"""核心时刻逻辑：跨日时刻解析、服务日判定、末班配对。

该模块不依赖网络，方便单元测试。时刻表抓取由 :mod:`commuter.scraper` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

# 服务日类型
WEEKDAY = "weekday"   # 工作日（周一至周五始发）
WEEKEND = "weekend"   # 周末（周六、周日始发）
DAILY = "daily"       # 每日始发
SERVICE_KINDS = (WEEKDAY, WEEKEND, DAILY)
SERVICE_LABELS = {
    WEEKDAY: "工作日",
    WEEKEND: "周末",
    DAILY: "每日",
}

# 允许 24:xx / 25:xx … 形式的跨日时刻，上限 47:59（最多跨两天）
_TIME_RE = __import__("re").compile(r"^\s*(\d{1,2}):([0-5]\d)\s*$")


def parse_clock(value: str) -> timedelta | None:
    """把 ``HH:MM`` 解析为距服务日零点的时间差。

    支持 24:00 之后的跨日时刻（例如 ``25:10`` 表示次日 01:10）。
    无法解析时返回 ``None``，由调用方记为缺少关键时刻。
    """
    if value is None:
        return None
    match = _TIME_RE.match(str(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 47:
        return None
    return timedelta(hours=hour, minutes=minute)


def service_kind_for_day(day: date) -> str:
    """按始发服务日是星期几返回工作日/周末类型。"""
    return WEEKEND if day.weekday() >= 5 else WEEKDAY


def service_runs_on(service_kind: str, service_day: date) -> bool:
    """判断某类班次是否在给定始发服务日运行。"""
    if service_kind == DAILY:
        return True
    return service_kind == service_kind_for_day(service_day)


@dataclass
class StopRecord:
    """某车次在某站的一条停站记录（由分页表格解析得到）。"""

    trip_no: str
    stop_seq: int | None
    station: str
    clock: str          # 原始时刻文本，如 "24:30"
    service_kind: str   # weekday / weekend / daily
    source_url: str | None = None  # 该记录所在分页 URL（质检报告溯源用）

    def clock_delta(self) -> timedelta | None:
        return parse_clock(self.clock)


@dataclass
class TripOption:
    """核定通过的一个可选班次（某车次 + 某个始发服务日）。"""

    trip_no: str
    service_kind: str
    service_day: date
    board_seq: int
    alight_seq: int
    depart_dt: datetime          # 实际发车时间（含跨日换算）
    arrive_dt: datetime          # 实际到站时间（含跨日换算）
    depart_clock: str
    arrive_clock: str


@dataclass
class Determination:
    """末班核定结果。"""

    latest: TripOption | None = None
    options: list[TripOption] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _station_key(name: str) -> str:
    return "".join(str(name).split())


def determine_last_trip(
    board_records: Iterable[StopRecord],
    alight_records: Iterable[StopRecord],
    board_station: str,
    alight_station: str,
    earliest_depart: datetime,
    latest_arrive: datetime,
) -> Determination:
    """按车次号与停站顺序配对两站记录，核定可乘坐的最晚一班。

    筛选条件：
      1. 两站记录车次号相同，且上车站停站顺序早于下车站；
      2. 两站均有可解析的关键时刻，服务日类型一致；
      3. 在“最早出门 ~ 到岗截止”覆盖的始发服务日内，该班次当天运行；
      4. 实际发车不早于最早出门、实际到站不晚于到岗截止。

    24:xx 跨日时刻按 ``始发服务日零点 + 时间差`` 换算成真实日期时间。
    """
    result = Determination()
    board_key, alight_key = _station_key(board_station), _station_key(alight_station)

    board_map: dict[str, StopRecord] = {}
    alight_map: dict[str, StopRecord] = {}
    for rec in board_records:
        if _station_key(rec.station) == board_key:
            board_map.setdefault(rec.trip_no, rec)
    for rec in alight_records:
        if _station_key(rec.station) == alight_key:
            alight_map.setdefault(rec.trip_no, rec)

    if not board_map:
        result.problems.append(f"上车站「{board_station}」没有解析到任何车次记录。")
    if not alight_map:
        result.problems.append(f"下车站「{alight_station}」没有解析到任何车次记录。")

    # 上车站能看到、下车站却没有记录的车次，无法确认能否到岗，需提示数据不全
    for trip_no in sorted(set(board_map) - set(alight_map)):
        result.problems.append(
            f"车次 {trip_no} 在「{alight_station}」站时刻表中没有记录，无法核定该班次。"
        )

    # 从最早出门当天到截止当天都可能是始发服务日（跨日车次的零点在该区间内）
    first_day = earliest_depart.date()
    last_day = latest_arrive.date()
    candidate_days = [
        first_day + timedelta(days=i)
        for i in range((last_day - first_day).days + 1)
    ]

    for trip_no in sorted(set(board_map) & set(alight_map)):
        b_rec, a_rec = board_map[trip_no], alight_map[trip_no]

        if b_rec.stop_seq is None or a_rec.stop_seq is None:
            result.problems.append(f"车次 {trip_no} 缺少停站顺序，无法确认上下车先后。")
            continue
        if b_rec.stop_seq >= a_rec.stop_seq:
            result.problems.append(
                f"车次 {trip_no} 的停站顺序不满足「{board_station}」先于「{alight_station}」。"
            )
            continue
        if b_rec.service_kind != a_rec.service_kind:
            result.problems.append(
                f"车次 {trip_no} 在两站页面标注的服务日类型不一致，已跳过。"
            )
            continue

        b_delta, a_delta = b_rec.clock_delta(), a_rec.clock_delta()
        missing = []
        if b_delta is None:
            missing.append(f"「{board_station}」发车时刻")
        if a_delta is None:
            missing.append(f"「{alight_station}」到站时刻")
        if missing:
            result.problems.append(f"车次 {trip_no} 缺少关键时刻（{'、'.join(missing)}），无法核定。")
            continue

        # 停站顺序在下、时刻却更早（如 23:50 发车、01:00 到站且未用 25:00 书写），
        # 说明到站落在次日，补 24 小时；补完仍早于发车则时刻本身矛盾。
        if a_delta < b_delta:
            a_delta += timedelta(hours=24)
        if a_delta < b_delta:
            result.problems.append(
                f"车次 {trip_no} 两站时刻矛盾（到站早于发车且无法按跨日解释），无法核定。"
            )
            continue

        service_kind = b_rec.service_kind
        for service_day in candidate_days:
            if not service_runs_on(service_kind, service_day):
                continue
            base = datetime.combine(service_day, datetime.min.time())
            depart_dt = base + b_delta
            arrive_dt = base + a_delta
            if depart_dt < earliest_depart or arrive_dt > latest_arrive:
                continue
            result.options.append(
                TripOption(
                    trip_no=trip_no,
                    service_kind=service_kind,
                    service_day=service_day,
                    board_seq=b_rec.stop_seq,
                    alight_seq=a_rec.stop_seq,
                    depart_dt=depart_dt,
                    arrive_dt=arrive_dt,
                    depart_clock=b_rec.clock.strip(),
                    arrive_clock=a_rec.clock.strip(),
                )
            )

    result.options.sort(key=lambda opt: opt.depart_dt, reverse=True)
    result.latest = result.options[0] if result.options else None
    return result


# ---------------------------------------------------------------------------
# 线路时刻质检：按服务日类型与停站顺序对齐整条线路，查超时 / 缺站 / 站序冲突
# ---------------------------------------------------------------------------

# 质检问题类型
ISSUE_OVERTIME = "overtime"        # 站间耗时超限
ISSUE_MISSING = "missing_stop"     # 缺站 / 缺时刻
ISSUE_SEQ_CONFLICT = "seq_conflict"  # 站序冲突
ISSUE_AMBIGUOUS = "ambiguous"      # 同站同服务日多条记录无法唯一对应
ISSUE_TYPES = (ISSUE_OVERTIME, ISSUE_MISSING, ISSUE_SEQ_CONFLICT, ISSUE_AMBIGUOUS)

# 问题可信级别：CONFIRMED 为已确认异常；REVIEW 为待核实（依赖失败分页或数据歧义）
STATUS_CONFIRMED = "confirmed"
STATUS_REVIEW = "review"

ISSUE_LABELS = {
    ISSUE_OVERTIME: "站间超时",
    ISSUE_MISSING: "缺站/缺时刻",
    ISSUE_SEQ_CONFLICT: "站序冲突",
    ISSUE_AMBIGUOUS: "记录待核实",
}
STATUS_LABELS = {
    STATUS_CONFIRMED: "已确认异常",
    STATUS_REVIEW: "待核实",
}


@dataclass
class StationFetch:
    """某站分页抓取结果（由 Web 层调用抓取器后组装）。"""

    station: str
    records: list[StopRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    first_url: str | None = None


@dataclass
class QCIssue:
    """一条质检问题。"""

    kind: str
    status: str
    service_kind: str
    segment: str            # 涉及站段或站点的可读描述
    detail: str             # 具体说明
    sources: list[str] = field(default_factory=list)
    order: int = 0          # 排序用：按线路位置


@dataclass
class TimePoint:
    """对齐后的某站时刻点（按服务日类型分组）。"""

    station: str
    clock: str | None       # 原始时刻文本（保留 00:xx / 24:xx 混写）
    elapsed: timedelta | None  # 相对始发服务日零点连续换算后的累计时长
    day_offset: int | None     # 距始发服务日的天数（由混写/回卷换算得到）
    stop_seq: int | None
    source_url: str | None
    note: str = ""          # 缺站/待核实/时刻无法解析等备注


@dataclass
class LineQCResult:
    """整条线路对某一车次的质检结果。"""

    trip_no: str
    stations: list[str]
    max_minutes: list[int]
    issues: list[QCIssue] = field(default_factory=list)
    # 每种服务日类型一张连续时刻轴：service_kind -> [TimePoint]
    timelines: dict[str, list[TimePoint]] = field(default_factory=dict)
    # 各站抓取状态：station -> {"ok": bool, "errors": [...], "url": str|None}
    station_status: dict[str, dict] = field(default_factory=dict)

    @property
    def confirmed_issues(self) -> list[QCIssue]:
        return [i for i in self.issues if i.status == STATUS_CONFIRMED]

    @property
    def review_issues(self) -> list[QCIssue]:
        return [i for i in self.issues if i.status == STATUS_REVIEW]

    @property
    def any_station_failed(self) -> bool:
        return any(not s["ok"] for s in self.station_status.values())

    @property
    def line_normal(self) -> bool:
        """没有任何问题且所有站分页完整，才能断言全线正常。"""
        return not self.issues and not self.any_station_failed


def _segment_label(station_a: str, station_b: str) -> str:
    return f"{station_a} → {station_b}"


def check_line_timetable(
    trip_no: str,
    stations: list[str],
    max_minutes: list[int],
    fetches: list[StationFetch],
) -> LineQCResult:
    """对单个车次做整条线路的时刻质检（纯逻辑，不依赖网络）。

    :param trip_no: 要质检的车次号
    :param stations: 按行驶顺序排列的站名
    :param max_minutes: 相邻站间最长允许分钟数，长度为 ``len(stations) - 1``
    :param fetches: 与 ``stations`` 一一对应的各站抓取结果
    :return: :class:`LineQCResult`，含问题清单、连续时刻轴与各站抓取状态

    处理规则：
      * 按服务日类型（工作日/周末/每日）分别对齐记录，服务日标注不一致的车次
        会自然表现为某些服务日轴上的缺站；
      * 00:xx / 24:xx 混写的时刻沿线路连续换算为“始发服务日零点 + 累计时长”，
        后站累计时长不大于前站时按次日回卷（最多回卷一次，仍回卷不上记冲突）；
      * 缺站、超时、站序冲突标为已确认异常；同站同服务日无法唯一对应、
        依赖失败分页的站段标为待核实；
      * 某站分页失败只影响依赖该站的结论，其余站确认的异常照常保留，
        且本函数永不断言全线正常（由 :attr:`LineQCResult.line_normal` 体现）。
    """
    result = LineQCResult(trip_no=trip_no, stations=list(stations), max_minutes=list(max_minutes))

    for fetch in fetches:
        result.station_status[fetch.station] = {
            "ok": not fetch.errors,
            "errors": list(fetch.errors),
            "url": fetch.first_url,
        }

    kinds_present = set()
    # station_index -> service_kind -> 匹配该车次的记录列表
    grouped: list[dict[str, list[StopRecord]]] = []
    for fetch in fetches:
        per_kind: dict[str, list[StopRecord]] = {}
        for rec in fetch.records:
            if rec.trip_no == trip_no:
                per_kind.setdefault(rec.service_kind, []).append(rec)
                kinds_present.add(rec.service_kind)
        grouped.append(per_kind)

    def add_issue(kind, status, kind_label, segment, detail, sources, order):
        result.issues.append(
            QCIssue(
                kind=kind,
                status=status,
                service_kind=kind_label,
                segment=segment,
                detail=detail,
                sources=list(dict.fromkeys(s for s in sources if s)),
                order=order,
            )
        )

    for service_kind in sorted(kinds_present):
        kind_label = SERVICE_LABELS.get(service_kind, service_kind)
        # 每站一个状态槽位，承载对齐后的时刻点与站段可用性
        slots: list[TimePoint | None] = [None] * len(stations)
        # seg_ok[i] 表示站段 stations[i] -> stations[i+1] 是否可判定
        seg_ok = [True] * (len(stations) - 1)
        last_elapsed: timedelta | None = None

        for idx, station in enumerate(stations):
            order = idx + 1
            fetch = fetches[idx]
            page_errors = list(fetch.errors)
            page_url = fetch.first_url
            failed = bool(page_errors)
            matched = grouped[idx].get(service_kind, [])

            if failed:
                # 分页失败：依赖该站的相邻站段一律待核实，不臆造缺站结论
                slots[idx] = TimePoint(
                    station=station, clock=None, elapsed=None, day_offset=None,
                    stop_seq=None, source_url=page_url,
                    note="分页抓取失败，依赖本站的结果待核实",
                )
                if idx > 0:
                    seg_ok[idx - 1] = False
                if idx < len(seg_ok):
                    seg_ok[idx] = False
                add_issue(
                    ISSUE_MISSING, STATUS_REVIEW, kind_label, station,
                    f"「{station}」分页抓取失败，无法确认车次 {trip_no} 是否停靠本站及到发时刻"
                    f"（{ '；'.join(page_errors) }）。",
                    [page_url], order,
                )
                continue

            if not matched:
                slots[idx] = TimePoint(
                    station=station, clock=None, elapsed=None, day_offset=None,
                    stop_seq=None, source_url=page_url, note="缺站：未找到该车次记录",
                )
                if idx > 0:
                    seg_ok[idx - 1] = False
                if idx < len(seg_ok):
                    seg_ok[idx] = False
                add_issue(
                    ISSUE_MISSING, STATUS_CONFIRMED, kind_label, station,
                    f"车次 {trip_no}（{kind_label}）在「{station}」站时刻表中没有停站记录，判定为缺站。",
                    [page_url], order,
                )
                continue

            if len(matched) > 1:
                # 同站同服务日无法唯一对应：标待核实，相邻站段不做超时断言
                slots[idx] = TimePoint(
                    station=station, clock=None, elapsed=None, day_offset=None,
                    stop_seq=None,
                    source_url=matched[0].source_url or page_url,
                    note=f"待核实：同站同服务日存在 {len(matched)} 条记录",
                )
                if idx > 0:
                    seg_ok[idx - 1] = False
                if idx < len(seg_ok):
                    seg_ok[idx] = False
                clocks = "、".join(r.clock.strip() or "（无时刻）" for r in matched)
                add_issue(
                    ISSUE_AMBIGUOUS, STATUS_REVIEW, kind_label, station,
                    f"车次 {trip_no}（{kind_label}）在「{station}」站有 {len(matched)} 条"
                    f"同服务日记录（时刻 {clocks}），无法唯一对应，需人工核实。",
                    [r.source_url or page_url for r in matched], order,
                )
                continue

            rec = matched[0]
            source = rec.source_url or page_url
            delta = rec.clock_delta()
            if delta is None:
                slots[idx] = TimePoint(
                    station=station, clock=rec.clock, elapsed=None, day_offset=None,
                    stop_seq=rec.stop_seq, source_url=source,
                    note="时刻无法解析，缺关键时刻",
                )
                if idx > 0:
                    seg_ok[idx - 1] = False
                if idx < len(seg_ok):
                    seg_ok[idx] = False
                add_issue(
                    ISSUE_MISSING, STATUS_CONFIRMED, kind_label, station,
                    f"车次 {trip_no}（{kind_label}）在「{station}」站的时刻"
                    f"「{rec.clock or '（空）'}」无法解析，缺少有效到发时刻。",
                    [source], order,
                )
                continue

            # 连续换算：与前一有效时刻比较，00:xx 早于前站（如 24:40）时按次日回卷
            elapsed = delta
            day_offset = 0
            if last_elapsed is not None and elapsed <= last_elapsed:
                elapsed += timedelta(hours=24)
            if last_elapsed is not None and elapsed <= last_elapsed:
                add_issue(
                    ISSUE_SEQ_CONFLICT, STATUS_CONFIRMED, kind_label, station,
                    f"车次 {trip_no}（{kind_label}）在「{station}」站时刻「{rec.clock.strip()}」"
                    f"按跨日回卷后仍不晚于前站时刻，时刻顺序矛盾。",
                    [source], order,
                )
            if elapsed is not None:
                day_offset = int(elapsed.total_seconds() // 86400)
            slots[idx] = TimePoint(
                station=station,
                clock=rec.clock.strip(),
                elapsed=elapsed,
                day_offset=day_offset,
                stop_seq=rec.stop_seq,
                source_url=source,
            )
            last_elapsed = elapsed

            # 停站顺序检查：缺失或与行驶顺序不一致都属站序冲突
            if rec.stop_seq is None:
                add_issue(
                    ISSUE_SEQ_CONFLICT, STATUS_CONFIRMED, kind_label, station,
                    f"车次 {trip_no}（{kind_label}）在「{station}」站缺少停站顺序，"
                    f"无法核对站序。",
                    [source], order,
                )
            elif rec.stop_seq != order:
                add_issue(
                    ISSUE_SEQ_CONFLICT, STATUS_CONFIRMED, kind_label, station,
                    f"车次 {trip_no}（{kind_label}）在「{station}」站标注停站顺序为 "
                    f"{rec.stop_seq}，与行驶顺序第 {order} 站不符。",
                    [source], order,
                )

        result.timelines[service_kind] = slots

        # 站间超时：仅当相邻两站时刻都有效、且站段可判定时才下结论
        for i in range(len(stations) - 1):
            left, right = slots[i], slots[i + 1]
            segment = _segment_label(stations[i], stations[i + 1])
            if not seg_ok[i]:
                # 不重复造问题：失败/缺站/歧义本身已登记，这里只在尚未有任何
                # 覆盖该站段的问题时补一条待核实说明
                covered = any(
                    iss.service_kind == kind_label
                    and iss.order in (i + 1, i + 2)
                    and iss.kind in (ISSUE_MISSING, ISSUE_AMBIGUOUS)
                    for iss in result.issues
                )
                if not covered:
                    add_issue(
                        ISSUE_OVERTIME, STATUS_REVIEW, kind_label, segment,
                        f"站段 {segment} 存在不可用记录，站间耗时无法核算，待核实。",
                        [left.source_url if left else None,
                         right.source_url if right else None],
                        i + 1,
                    )
                continue
            if left is None or right is None or left.elapsed is None or right.elapsed is None:
                continue
            minutes = int((right.elapsed - left.elapsed).total_seconds() // 60)
            limit = max_minutes[i]
            if minutes > limit:
                add_issue(
                    ISSUE_OVERTIME, STATUS_CONFIRMED, kind_label, segment,
                    f"站段 {segment} 实际耗时 {minutes} 分钟"
                    f"（{left.clock} → {right.clock}），超过最长允许 {limit} 分钟。",
                    [left.source_url, right.source_url], i + 1,
                )

    # 排序：先按线路位置，再按问题类型，便于站务逐段核对
    result.issues.sort(key=lambda iss: (iss.order, iss.kind, iss.service_kind))
    return result
