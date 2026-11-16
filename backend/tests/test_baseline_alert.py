"""基线预警重算链路的确定性测试。

只依赖标准库 unittest，直接驱动服务层，固定重算时刻
（2026-11-16T10:00:00Z，北京时间 18:00），避免受系统时钟影响。

覆盖验收点：
1. 跨时区承诺时间统一 UTC 口径；
2. 周末/节假日偏移修正，禁止自然日直接破线；
3. 规则批次冲突按晚到版本裁决，且历史判定延续（已完成运行冻结）；
4. 正常/即将破线/已破线三档边界；
5. 晚到依赖触发修正已生成预警；
6. 重复扫描同一关键下游只通知一次；
7. 基线管理与依赖解析读同一份预警水位；
8. 既有记录兼容（老字段占位文本、冻结历史记录不被覆盖）。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services import calendar_rules as cal
from app.services.baseline_alert import (
    ALERT_MODULE,
    TRIGGER_MODULE,
    BaselineAlertService,
)
from app.store import store

NOW = datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc)
SH = ZoneInfo("Asia/Shanghai")


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.svc = BaselineAlertService()

    def _alerts_by_baseline(self) -> dict[str, dict]:
        return {row["基线编号"]: row for row in store.rows(ALERT_MODULE)}

    def _baseline(self, baseline_no: str) -> dict:
        return next(row for row in store.rows("baseline")
                    if row["基线编号"] == baseline_no)

    def _runtime(self, runtime_no: str) -> dict:
        return next(row for row in store.rows("runtime")
                    if row["记录编号"] == runtime_no)


class CalendarRuleTests(BaseCase):
    def test_parse_timezone_normalization(self) -> None:
        # 纽约时间 11-17 18:00（UTC-5）应换算为 UTC 11-17 23:00
        dt = cal.parse_datetime("2026-11-17T18:00:00-05:00")
        self.assertEqual(cal.to_utc(dt).isoformat(),
                         "2026-11-17T23:00:00+00:00")
        # 裸日期按批次时区当天下班时刻解释
        bare = cal.parse_datetime("2026-11-16", tz_name="Asia/Shanghai")
        self.assertEqual(cal.to_utc(bare).isoformat(),
                         "2026-11-16T15:59:59+00:00")
        # 非法时区回退默认时区，不抛异常
        self.assertEqual(cal.parse_timezone("Not/AZone").key, "Asia/Shanghai")

    def test_batch_conflict_latest_arrival_wins(self) -> None:
        from datetime import date

        batch = cal.select_batch(date(2026, 11, 18))
        self.assertEqual(batch["规则版本"], "工作日历2026-V2")
        # V2 把 11-18 定为节假日；V1 没有 → 冲突日期按晚到的 V2
        self.assertFalse(cal.is_workday(date(2026, 11, 18), batch))
        # 11-21（周六）在 V2 补班 → 工作日
        self.assertTrue(cal.is_workday(date(2026, 11, 21), batch))
        # 草稿批次永远不参与裁决
        self.assertTrue(all(r["规则版本"] != "工作日历2026-DRAFT"
                            for r in cal.list_active_batches()))

    def test_holiday_shift_forward(self) -> None:
        from datetime import date

        batch = cal.select_batch(date(2026, 11, 14))
        dt_local = datetime(2026, 11, 14, 12, 0, tzinfo=SH)
        shifted, detail = cal.shift_commitment(dt_local, batch, shift_mode="顺延")
        # 11-14(周六)、11-15(周日) 均非工作日 → 顺延到 11-16(周一)
        self.assertEqual(shifted.date().isoformat(), "2026-11-16")
        self.assertEqual(detail["偏移工作日数"], 2)

    def test_workday_minutes_exclude_weekend(self) -> None:
        batch = cal.select_batch(datetime(2026, 11, 13).date())
        start = datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)  # 北京 11-14 06:00
        end = datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc)    # 北京 11-16 00:00
        # 整个区间落在周六/周日（V2 均为节假日），工作日余量必须为 0，
        # 而不是旧自然日口径的 2760 分钟
        self.assertEqual(cal.workday_minutes_between(start, end, batch), 0)
        # 对照：再多跨到周一 06:00（UTC 11-15 22:00），只计入周一的 6 小时 = 360 分钟
        end2 = datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(cal.workday_minutes_between(start, end2, batch), 360)


class RecomputeTests(BaseCase):
    def test_three_water_levels_and_no_natural_day_breach(self) -> None:
        self.svc.recompute(now=NOW)
        alerts = self._alerts_by_baseline()

        # BL-101：承诺 UTC 15:59:59，剩余 359 分钟，余量阈值 300 → 正常；
        # 但其关键下游 JOB-DWS-核心汇总触发延迟 → 抬升到即将破线
        a101 = alerts["BL-101"]
        self.assertEqual(a101["预警水位"], cal.WATER_WARNING)
        self.assertTrue(a101["关键下游延迟"])
        self.assertGreater(a101["剩余分钟"], 300)  # 旧自然日口径会直接判破线/异常

        # BL-102：承诺 11-17 12:00 UTC，剩余 1560 分钟，阈值 120 → 正常
        a102 = alerts["BL-102"]
        self.assertEqual(a102["预警水位"], cal.WATER_NORMAL)
        self.assertFalse(a102["关键下游延迟"])  # 该下游早于应到时间触发

        # BL-103：跨时区，纽约 11-17 18:00 → UTC 23:00，剩余 2100 分钟 → 正常
        a103 = alerts["BL-103"]
        self.assertEqual(a103["承诺时间UTC"], "2026-11-17T23:00:00+00:00")
        self.assertEqual(a103["标准时区"], "Asia/Shanghai")
        self.assertEqual(a103["预警水位"], cal.WATER_NORMAL)

        # BL-104：承诺落在节假日，顺延两个自然日到 11-16 04:00 UTC
        a104 = alerts["BL-104"]
        self.assertEqual(a104["节假日偏移明细"]["偏移后承诺日期"], "2026-11-16")
        # 顺延之后承诺 UTC 04:00，NOW=10:00 已超过 → 已破线（按修正后时刻判定）
        self.assertEqual(a104["预警水位"], cal.WATER_BREACHED)
        self.assertLess(a104["剩余分钟"], 0)

    def test_warning_boundary_uses_adjusted_commitment(self) -> None:
        # 把 BL-101 余量调成刚好覆盖剩余 359 分钟 → 进入「即将破线」边界
        baseline = self._baseline("BL-101")
        baseline["预警余量分钟"] = 359
        self.svc.recompute(now=NOW)
        a = self._alerts_by_baseline()["BL-101"]
        self.assertEqual(a["预警水位"], cal.WATER_WARNING)

        baseline["预警余量分钟"] = 358
        self.svc.recompute(now=NOW)
        # 即便去掉关键下游抬升，359 剩余 > 358 余量，仍因下游延迟保持即将破线
        # 再清掉延迟触发，验证纯余量边界：359 > 358 → 正常
        store.rows(TRIGGER_MODULE).clear()
        self.svc.recompute(now=NOW)
        a = self._alerts_by_baseline()["BL-101"]
        self.assertEqual(a["预警水位"], cal.WATER_NORMAL)

    def test_late_trigger_corrects_generated_alert(self) -> None:
        self.svc.recompute(now=NOW)
        a = self._alerts_by_baseline()["BL-101"]
        self.assertTrue(a["关键下游延迟"])
        self.assertIn("JOB-DWS-核心汇总", a["已通知关键下游"])

        # 晚到的修正触发：同一下游新时刻早于应到时间
        entry, msg = self.svc.ingest_trigger({
            "下游作业": "JOB-DWS-核心汇总",
            "上游作业": "JOB-ADS-汇总",
            "触发时间": "2026-11-16T07:30:00+00:00",
            "应到时间": "2026-11-16T08:00:00+00:00",
            "关键下游标志": True,
            "时区": "UTC",
            "到达批次": "BATCH-20261116-02",
        })
        self.assertTrue(entry)
        self.svc.recompute(now=NOW)
        a = self._alerts_by_baseline()["BL-101"]
        self.assertFalse(a["关键下游延迟"])
        self.assertEqual(a["已通知关键下游"], [])  # 延迟事件关闭，台账清出
        self.assertEqual(a["预警水位"], cal.WATER_NORMAL)
        # 修正轨迹留痕
        self.assertGreaterEqual(len(a["历史判定"]), 2)

    def test_duplicate_scan_notifies_downstream_once(self) -> None:
        self.svc.recompute(now=NOW)
        a1 = self._alerts_by_baseline()["BL-101"]
        notified_v1 = list(a1["已通知关键下游"])
        versions = a1["重算版本"]

        # 重复扫描（无新数据再重算两次），台账与通知计数不重复膨胀
        self.svc.recompute(now=NOW)
        self.svc.recompute(now=NOW)
        a2 = self._alerts_by_baseline()["BL-101"]
        self.assertEqual(a2["已通知关键下游"], notified_v1)
        self.assertEqual(len(a2["已通知关键下游"]), 1)
        self.assertEqual(a2["关键下游延迟次数"], 1)
        self.assertGreater(a2["重算版本"], versions)  # 版本仍递增，结果被刷新而非新增

        # 同一下游同一触发时刻重复上报 → 幂等，不新增触发数据
        before = len(store.rows(TRIGGER_MODULE))
        _, message = self.svc.ingest_trigger({
            "下游作业": "JOB-DWS-核心汇总",
            "触发时间": "2026-11-16T09:30:00+00:00",
            "应到时间": "2026-11-16T08:00:00+00:00",
            "关键下游标志": True,
        })
        self.assertIn("幂等", message)
        self.assertEqual(len(store.rows(TRIGGER_MODULE)), before)

    def test_recompute_does_not_duplicate_alert_records(self) -> None:
        self.svc.recompute(now=NOW)
        first_count = len(store.rows(ALERT_MODULE))
        self.svc.recompute(now=NOW)
        self.svc.recompute(now=NOW)
        self.assertEqual(len(store.rows(ALERT_MODULE)), first_count)

    def test_finished_runtime_keeps_frozen_history(self) -> None:
        # 种子里的冻结记录对应运行成功；重算不得覆盖其历史判定
        frozen_before = next(r for r in store.rows(ALERT_MODULE)
                             if r["基线编号"] == "BASE-FROZEN-01")
        history_len = len(frozen_before["历史判定"])
        result = self.svc.recompute(now=NOW)
        self.assertGreaterEqual(result["stats"]["frozen"], 1)
        frozen_after = next(r for r in store.rows(ALERT_MODULE)
                            if r["基线编号"] == "BASE-FROZEN-01")
        self.assertTrue(frozen_after["冻结标志"])
        self.assertEqual(len(frozen_after["历史判定"]), history_len)
        self.assertEqual(frozen_after["预警水位"], cal.WATER_BREACHED)

    def test_unfinished_runtime_recomputed_on_rule_change(self) -> None:
        self.svc.recompute(now=NOW)
        a = self._alerts_by_baseline()["BL-104"]
        self.assertEqual(a["规则版本"], "工作日历2026-V2")

        # 模拟口径变更：V2 撤掉 11-14/11-15 节假日（新批次 V3 晚到）
        store.rows("calendar_rule_batch").append({
            "id": 4, "状态": "已生效", "规则版本": "工作日历2026-V3",
            "标准时区": "Asia/Shanghai", "工作日集合": [1, 2, 3, 4, 5],
            "节假日列表": [], "补班日列表": [],
            "生效起始日期": "2026-01-01", "生效截止日期": "2026-12-31",
            "到达时间": "2026-11-10T00:00:00+00:00",
        })
        self.svc.recompute(now=NOW)
        a = self._alerts_by_baseline()["BL-104"]
        self.assertEqual(a["规则版本"], "工作日历2026-V3")
        # 新口径下 11-14 是普通周六，顺延仍到 11-16，偏移明细按新口径重算
        self.assertEqual(a["节假日偏移明细"]["偏移后承诺日期"], "2026-11-16")

    def test_baseline_disable_retires_alert_and_reenable_restores(self) -> None:
        self.svc.recompute(now=NOW)
        alert = self._alerts_by_baseline()["BL-101"]
        self.assertTrue(alert.get("生效标志", True))

        # 停用基线 → 联动重算 → 未冻结预警退役，共享视图不再读到它
        self._baseline("BL-101")["status"] = "已停用"
        result = self.svc.recompute(now=NOW)
        self.assertGreaterEqual(result["stats"]["retired"], 1)
        alert = self._alerts_by_baseline()["BL-101"]
        self.assertFalse(alert["生效标志"])
        timeline_jobs = {x["作业名称"] for x in self.svc.timeline_view()}
        self.assertNotIn("JOB-DWS-核心汇总", timeline_jobs)

        # 重新启用 → 下一轮重算恢复生效
        self._baseline("BL-101")["status"] = "已启用"
        self.svc.recompute(now=NOW)
        alert = self._alerts_by_baseline()["BL-101"]
        self.assertTrue(alert["生效标志"])
        timeline_jobs = {x["作业名称"] for x in self.svc.timeline_view()}
        self.assertIn("JOB-DWS-核心汇总", timeline_jobs)

    def test_legacy_records_compatible(self) -> None:
        # 老基线「基线管理样例3」的预警余量是占位文本，必须回退默认值而不是报错
        self.svc.recompute(now=NOW)
        # 它是已启用但关联作业为占位文本，无运行匹配 → 不产生预警也不抛异常
        alerts = self._alerts_by_baseline()
        self.assertNotIn("基线管理样例3", alerts)
        # 默认余量解析可直接验证
        legacy = self._baseline("BASE-0003")
        self.assertEqual(BaselineAlertService._warn_margin(legacy),
                         cal.DEFAULT_WARN_MARGIN_MINUTES)


class SharedWaterLevelTests(BaseCase):
    def test_dependency_and_timeline_share_same_water(self) -> None:
        self.svc.recompute(now=NOW)
        annotated = {row["下游作业"]: row
                     for row in self.svc.annotate_dependencies()}
        timeline = {row["作业名称"]: row
                    for row in self.svc.timeline_view()}

        row = annotated["JOB-DWS-核心汇总"]
        self.assertEqual(row["共享预警水位"], cal.WATER_WARNING)
        self.assertTrue(row["关键下游延迟"])
        # 同一份预警结果 id，跨模块可核对
        self.assertIsNotNone(row["来源预警结果id"])
        self.assertEqual(row["来源预警结果id"],
                         timeline["JOB-DWS-核心汇总"]["预警结果id"])
        self.assertEqual(row["共享预警水位"],
                         timeline["JOB-DWS-核心汇总"]["预警水位"])
        self.assertIn("UTC", row["水位口径"])

        # 无预警关联的老种子依赖关系回落到「正常」，不报错
        old = annotated["依赖解析样例1"]
        self.assertEqual(old["共享预警水位"], cal.WATER_NORMAL)
        self.assertIsNone(old["来源预警结果id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
