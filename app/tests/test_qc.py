"""线路时刻质检单元测试：连续换算、超时、缺站、站序冲突、待核实与分页失败。"""

import unittest
from datetime import timedelta

from commuter.times import (
    DAILY,
    WEEKDAY,
    WEEKEND,
    ISSUE_AMBIGUOUS,
    ISSUE_MISSING,
    ISSUE_OVERTIME,
    ISSUE_SEQ_CONFLICT,
    STATUS_CONFIRMED,
    STATUS_REVIEW,
    StationFetch,
    StopRecord,
    check_line_timetable,
)

STATIONS = ["滨河园区", "东门枢纽", "中央厂区", "夜班宿舍"]
LIMITS = [30, 50, 50]
URL = "http://example.invalid/station/{}/page-1"


def rec(trip_no, seq, station, clock, kind=DAILY):
    return StopRecord(trip_no, seq, station, clock, kind, URL.format(station))


def fetch(station, records, errors=None):
    return StationFetch(station, records, errors or [], URL.format(station))


def kinds(issues, kind, station_or_segment=None):
    out = [
        i for i in issues
        if i.kind == kind and (station_or_segment is None or station_or_segment in i.segment)
    ]
    return out


class MixedClockTests(unittest.TestCase):
    def test_mixed_00_and_24_clocks_roll_forward_continuously(self):
        # N301 同款数据：23:40 / 24:05 / 00:45 / 01:20 混写
        fetches = [
            fetch(s, [rec("N301", i + 1, s, clock)])
            for i, (s, clock) in enumerate([
                ("滨河园区", "23:40"), ("东门枢纽", "24:05"),
                ("中央厂区", "00:45"), ("夜班宿舍", "01:20"),
            ])
        ]
        result = check_line_timetable("N301", STATIONS, LIMITS, fetches)
        self.assertEqual(result.issues, [])
        self.assertTrue(result.line_normal)
        points = result.timelines[DAILY]
        self.assertEqual(
            [p.elapsed for p in points],
            [
                timedelta(hours=23, minutes=40),
                timedelta(hours=24, minutes=5),
                timedelta(hours=24, minutes=45),
                timedelta(hours=25, minutes=20),
            ],
        )
        self.assertEqual([p.day_offset for p in points], [0, 1, 1, 1])

    def test_24_style_writing_also_supported(self):
        fetches = [
            fetch(s, [rec("T1", i + 1, s, clock)])
            for i, (s, clock) in enumerate([
                ("滨河园区", "24:40"), ("东门枢纽", "25:05"),
                ("中央厂区", "25:50"), ("夜班宿舍", "26:20"),
            ])
        ]
        result = check_line_timetable("T1", STATIONS, LIMITS, fetches)
        self.assertTrue(result.line_normal)
        self.assertEqual(
            result.timelines[DAILY][-1].elapsed, timedelta(hours=26, minutes=20)
        )


class OvertimeTests(unittest.TestCase):
    def test_overtime_segment_confirmed_with_sources(self):
        fetches = [
            fetch(s, [rec("N302", i + 1, s, clock, WEEKDAY)])
            for i, (s, clock) in enumerate([
                ("滨河园区", "22:10"), ("东门枢纽", "22:35"),
                ("中央厂区", "23:10"), ("夜班宿舍", "24:20"),
            ])
        ]
        result = check_line_timetable("N302", STATIONS, LIMITS, fetches)
        overtime = kinds(result.issues, ISSUE_OVERTIME)
        self.assertEqual(len(overtime), 1)
        issue = overtime[0]
        self.assertEqual(issue.status, STATUS_CONFIRMED)
        self.assertIn("中央厂区 → 夜班宿舍", issue.segment)
        self.assertIn("70 分钟", issue.detail)
        self.assertEqual(len(issue.sources), 2)
        # 其他站段未超限
        self.assertFalse(result.any_station_failed)
        self.assertFalse(result.line_normal)


class MissingStopTests(unittest.TestCase):
    def test_missing_station_record_confirmed(self):
        rows = {
            "滨河园区": rec("N303", 1, "滨河园区", "23:20", WEEKEND),
            "东门枢纽": rec("N303", 2, "东门枢纽", "23:50", WEEKEND),
            "中央厂区": None,
            "夜班宿舍": rec("N303", 4, "夜班宿舍", "01:15", WEEKEND),
        }
        fetches = [
            fetch(s, [rows[s]] if rows[s] else []) for s in STATIONS
        ]
        result = check_line_timetable("N303", STATIONS, LIMITS, fetches)
        missing = kinds(result.issues, ISSUE_MISSING, "中央厂区")
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].status, STATUS_CONFIRMED)
        # 缺站两侧站段不做超时判定，也不额外编造超时问题
        self.assertEqual(kinds(result.issues, ISSUE_OVERTIME), [])
        # 时刻轴上缺站位置保留空洞，后续站仍按次日换算
        points = result.timelines[WEEKEND]
        self.assertIsNone(points[2].elapsed)
        self.assertEqual(points[3].elapsed, timedelta(hours=25, minutes=15))

    def test_unparseable_clock_is_missing_issue(self):
        fetches = [
            fetch(s, [rec("T9", i + 1, s, clock)])
            for i, (s, clock) in enumerate([
                ("滨河园区", "22:00"), ("东门枢纽", "22:20"),
                ("中央厂区", "待定"), ("夜班宿舍", "23:30"),
            ])
        ]
        result = check_line_timetable("T9", STATIONS, LIMITS, fetches)
        missing = kinds(result.issues, ISSUE_MISSING, "中央厂区")
        self.assertEqual(len(missing), 1)
        self.assertIn("无法解析", missing[0].detail)


class SeqConflictTests(unittest.TestCase):
    def test_wrong_stop_sequence_confirmed(self):
        fetches = [
            fetch(s, [rec("N304", seq, s, clock)])
            for s, seq, clock in [
                ("滨河园区", 1, "24:10"), ("东门枢纽", 3, "24:45"),
                ("中央厂区", 3, "25:20"), ("夜班宿舍", 4, "25:55"),
            ]
        ]
        result = check_line_timetable("N304", STATIONS, LIMITS, fetches)
        conflicts = kinds(result.issues, ISSUE_SEQ_CONFLICT, "东门枢纽")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].status, STATUS_CONFIRMED)
        self.assertIn("停站顺序为 3", conflicts[0].detail)
        self.assertIn("第 2 站", conflicts[0].detail)

    def test_missing_stop_sequence_confirmed(self):
        fetches = [
            fetch(STATIONS[0], [rec("T3", 1, STATIONS[0], "22:00")]),
            fetch(STATIONS[1], [StopRecord("T3", None, STATIONS[1], "22:25", DAILY,
                                           URL.format(STATIONS[1]))]),
            fetch(STATIONS[2], [rec("T3", 3, STATIONS[2], "23:00")]),
            fetch(STATIONS[3], [rec("T3", 4, STATIONS[3], "23:40")]),
        ]
        result = check_line_timetable("T3", STATIONS, LIMITS, fetches)
        self.assertTrue(kinds(result.issues, ISSUE_SEQ_CONFLICT, STATIONS[1]))


class AmbiguousTests(unittest.TestCase):
    def test_duplicate_same_station_same_kind_is_review(self):
        fetches = [
            fetch(STATIONS[0], [rec("N305", 1, STATIONS[0], "22:50")]),
            fetch(STATIONS[1], [rec("N305", 2, STATIONS[1], "23:20")]),
            fetch(STATIONS[2], [
                rec("N305", 3, STATIONS[2], "23:55"),
                rec("N305", 3, STATIONS[2], "24:05"),
            ]),
            fetch(STATIONS[3], [rec("N305", 4, STATIONS[3], "24:40")]),
        ]
        result = check_line_timetable("N305", STATIONS, LIMITS, fetches)
        ambiguous = kinds(result.issues, ISSUE_AMBIGUOUS)
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0].status, STATUS_REVIEW)
        self.assertIn("2 条", ambiguous[0].detail)
        # 相邻站段不得下超时断言
        self.assertEqual(kinds(result.issues, ISSUE_OVERTIME), [])
        # 时刻轴该站为空槽，备注说明待核实
        self.assertIn("待核实", result.timelines[DAILY][2].note)


class PaginationFailureTests(unittest.TestCase):
    def test_failed_station_marks_dependent_segments_review_but_keeps_other_findings(self):
        # 东门枢纽分页失败；同时滨河园区→东门枢纽之外另有一个已确认超时
        fetches = [
            fetch(STATIONS[0], [rec("T5", 1, STATIONS[0], "22:00")]),
            fetch(STATIONS[1], [], errors=["第 2 页抓取失败（http://x）：500"]),
            fetch(STATIONS[2], [rec("T5", 3, STATIONS[2], "22:40")]),
            # 中央厂区 22:40 → 夜班宿舍 23:50 = 70 分钟，超过 50
            fetch(STATIONS[3], [rec("T5", 4, STATIONS[3], "23:50")]),
        ]
        result = check_line_timetable("T5", STATIONS, LIMITS, fetches)

        self.assertTrue(result.any_station_failed)
        self.assertFalse(result.line_normal)

        review_missing = [
            i for i in result.issues
            if i.kind == ISSUE_MISSING and i.status == STATUS_REVIEW and STATIONS[1] in i.segment
        ]
        self.assertEqual(len(review_missing), 1)
        self.assertIn("分页抓取失败", review_missing[0].detail)

        # 不受失败站影响的站段，已确认超时仍然保留
        overtime = kinds(result.issues, ISSUE_OVERTIME, "中央厂区 → 夜班宿舍")
        self.assertEqual(len(overtime), 1)
        self.assertEqual(overtime[0].status, STATUS_CONFIRMED)

        # 依赖失败站的两个站段没有已确认结论
        dependent = [
            i for i in result.issues
            if i.kind == ISSUE_OVERTIME and i.status == STATUS_CONFIRMED
            and ("滨河园区 → 东门枢纽" in i.segment or "东门枢纽 → 中央厂区" in i.segment)
        ]
        self.assertEqual(dependent, [])

    def test_all_failed_never_claims_normal(self):
        fetches = [
            fetch(s, [], errors=["第 1 页抓取失败：500"]) for s in STATIONS
        ]
        result = check_line_timetable("T6", STATIONS, LIMITS, fetches)
        self.assertFalse(result.line_normal)
        self.assertTrue(all(i.status == STATUS_REVIEW for i in result.issues))


class ServiceKindTests(unittest.TestCase):
    def test_different_service_kinds_aligned_separately(self):
        # 同一车次在不同站标注了不同服务日：各自成轴，缺站显式暴露
        fetches = [
            fetch(STATIONS[0], [rec("T7", 1, STATIONS[0], "22:00", WEEKDAY)]),
            fetch(STATIONS[1], [rec("T7", 2, STATIONS[1], "22:25", WEEKDAY)]),
            fetch(STATIONS[2], [rec("T7", 3, STATIONS[2], "23:00", WEEKEND)]),
            fetch(STATIONS[3], [rec("T7", 4, STATIONS[3], "23:40", WEEKEND)]),
        ]
        result = check_line_timetable("T7", STATIONS, LIMITS, fetches)
        weekday_missing = [
            i for i in result.issues
            if i.kind == ISSUE_MISSING and i.service_kind == "工作日"
        ]
        weekend_missing = [
            i for i in result.issues
            if i.kind == ISSUE_MISSING and i.service_kind == "周末"
        ]
        self.assertTrue(weekday_missing)
        self.assertTrue(weekend_missing)
        self.assertEqual(set(result.timelines), {WEEKDAY, WEEKEND})


if __name__ == "__main__":
    unittest.main()
