"""端到端冒烟测试：用 Flask 测试客户端驱动示例站点 + 核定应用。

仅在安装了两个项目的依赖（Flask/requests/bs4）后可运行：
    python -m unittest tests.test_smoke
"""

import sys
import unittest
from pathlib import Path

import requests
from werkzeug.serving import make_server

# 让示例站点模块可导入
EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "example-site"
sys.path.insert(0, str(EXAMPLE_DIR))
import app as example_app  # noqa: E402

from commuter.app import app as commuter_app  # noqa: E402


class _Server:
    def __init__(self, flask_app, port):
        self.httpd = make_server("127.0.0.1", port, flask_app)
        self.thread = None

    def start(self):
        import threading

        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()


class SmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(example_app.app, 5051)
        cls.server.start()
        cls.base = "http://127.0.0.1:5051/"
        # 确认服务起来了
        requests.get(cls.base, timeout=5).raise_for_status()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _post(self, **overrides):
        payload = {
            "home_url": self.base,
            "board_station": "滨河园区",
            "alight_station": "中央厂区",
            "earliest_depart": "2026-11-11T22:00",
            "latest_arrive": "2026-11-12T02:00",
        }
        payload.update(overrides)
        return commuter_app.test_client().post("/", data=payload)

    def test_full_query_finds_latest(self):
        html = self._post().get_data(as_text=True)
        self.assertIn("N205", html)
        # 主结论正常给出；N206 数据不全只出现在告警区，不阻断核定
        self.assertNotIn("⚠ 无法核定末班", html)
        self.assertIn("数据告警", html)
        self.assertIn("N206", html)
        self.assertIn("2026-11-12 00:40", html)
        self.assertIn("2026-11-12 01:50", html)

    def test_pagination_failure_station(self):
        html = self._post(
            board_station="故障北站",
            earliest_depart="2026-11-11T21:00",
            latest_arrive="2026-11-12T05:00",
        ).get_data(as_text=True)
        self.assertIn("无法核定末班", html)
        self.assertIn("抓取失败", html)

    def test_unknown_station(self):
        html = self._post(board_station="不存在的站").get_data(as_text=True)
        self.assertIn("找不到站点", html)

    def test_missing_clock_trip_warns_but_others_valid(self):
        # N206 在中央厂区完全没有停站记录，但 N205 等其他车次仍可核定
        html = self._post(
            board_station="滨河园区",
            alight_station="中央厂区",
            earliest_depart="2026-11-11T22:00",
            latest_arrive="2026-11-12T03:00",
        ).get_data(as_text=True)
        self.assertIn("数据告警", html)
        self.assertIn("N206", html)
        self.assertIn("无法核定该班次", html)

    def test_validation_errors(self):
        client = commuter_app.test_client()
        html = client.post("/", data={}).get_data(as_text=True)
        self.assertIn("请填写", html)


class QualityCheckSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(example_app.app, 5052)
        cls.server.start()
        cls.base = "http://127.0.0.1:5052/"
        requests.get(cls.base, timeout=5).raise_for_status()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _qc(self, trip_no, stations=None, minutes=None, **overrides):
        payload = {
            "qc_home_url": self.base,
            "qc_trip_no": trip_no,
            "qc_stations": stations or "滨河园区\n东门枢纽\n中央厂区\n夜班宿舍",
            "qc_max_minutes": minutes or "30\n50\n50",
        }
        payload.update(overrides)
        return commuter_app.test_client().post("/qc", data=payload)

    def test_normal_trip_passes_with_mixed_clocks(self):
        html = self._qc("N301").get_data(as_text=True)
        self.assertIn("全线质检通过", html)
        # 混写 00:45 被连续换算到次日
        self.assertIn("次日 00:45", html)
        self.assertIn("次日 01:20", html)

    def test_overtime_trip_reports_confirmed_issue(self):
        html = self._qc("N302").get_data(as_text=True)
        self.assertIn("站间超时", html)
        self.assertIn("已确认异常", html)
        self.assertIn("70 分钟", html)
        self.assertIn("中央厂区 → 夜班宿舍", html)

    def test_missing_stop_trip(self):
        html = self._qc("N303").get_data(as_text=True)
        self.assertIn("缺站/缺时刻", html)
        self.assertIn("中央厂区", html)

    def test_seq_conflict_trip(self):
        html = self._qc("N304").get_data(as_text=True)
        self.assertIn("站序冲突", html)
        self.assertIn("停站顺序为 3", html)

    def test_ambiguous_trip_marks_review(self):
        html = self._qc("N305").get_data(as_text=True)
        self.assertIn("待核实", html)
        self.assertIn("无法唯一对应", html)
        # 歧义站相邻站段在耗时核算表中不得被标为“未超限”
        self.assertIn("站间耗时无法核算", html)

    def test_pagination_failure_keeps_other_confirmed_findings(self):
        # 故障北站分页 500：依赖它的站段待核实，且页面不能断言全线正常
        html = self._qc(
            "X901",
            stations="故障北站\n中央厂区",
            minutes="120",
        ).get_data(as_text=True)
        self.assertIn("抓取失败", html)
        self.assertIn("待核实", html)
        self.assertNotIn("全线质检通过", html)

    def test_qc_validation_errors(self):
        client = commuter_app.test_client()
        html = client.post("/qc", data={}).get_data(as_text=True)
        self.assertIn("请填写", html)

    def test_qc_unknown_trip(self):
        html = self._qc("NO-SUCH-TRIP").get_data(as_text=True)
        self.assertIn("均未找到车次", html)


if __name__ == "__main__":
    unittest.main()
