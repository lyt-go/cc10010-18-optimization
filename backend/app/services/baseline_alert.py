"""基线预警重算引擎。

输入（读取，不在本模块维护）：
- 基线定义 ``baseline``：承诺完成时间、预警余量、关联作业、节假日偏移口径；
- 依赖触发数据 ``dependency_trigger``：关键下游实际/预计触发时刻，晚到即修正；
- 运行实例 ``runtime``：判定运行是否已完成（完成后的历史判定冻结保留）；
- 日期规则批次 ``calendar_rule_batch``：工作日规则、节假日条件、统一时区，
  批次冲突由 :mod:`app.services.calendar_rules` 按晚到版本裁决。

输出（全平台唯一一份基线预警结果数据）：
- ``baseline_alert``：基线管理与依赖解析两个模块共享同一份预警水位，
  依赖解析不再自行判定破线。

旧实现的两个问题在这里被显式修掉：
1. 不再按自然日相减判破线——周末/月末/节假日偏移先把承诺时刻修正到工作日，
   再在统一 UTC 口径下比较；自然日差只作展示字段，不参与破线判定。
2. 同一关键下游重复扫描只生效一次（按下游作业记账），不会再产生重复预警；
   晚到触发把延迟修正回来后水位随之回落，并留修正痕迹。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import calendar_rules as cal
from app.store import store

BASELINE_MODULE = "baseline"
RUNTIME_MODULE = "runtime"
TRIGGER_MODULE = "dependency_trigger"
ALERT_MODULE = "baseline_alert"

# 运行实例进入这些状态后视为完成：预警结果冻结，规则口径变更也不再重算
FINISHED_RUNTIME_STATUSES = {"成功", "失败", "已取消", "已终止", "已完成"}
# 只有启用态基线才参与重算，草稿/评审/停用基线不产出现网预警
ACTIVE_BASELINE_STATUSES = {"已启用"}


class BaselineAlertService:
    # ------------------------------------------------------------------ 读取
    def list_alerts(
        self,
        *,
        keyword: str | None = None,
        water_level: str | None = None,
        active_only: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(ALERT_MODULE)
        if active_only:
            rows = [row for row in rows if row.get("生效标志", True)]
        if keyword:
            rows = [row for row in rows
                    if keyword in str(row.get("基线编号", ""))
                    or keyword in str(row.get("作业名称", ""))]
        if water_level:
            rows = [row for row in rows if row.get("预警水位") == water_level]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_alert(self, alert_id: int) -> dict[str, Any] | None:
        return store.find(ALERT_MODULE, alert_id)

    def list_triggers(self) -> list[dict[str, Any]]:
        return list(store.rows(TRIGGER_MODULE))

    # ------------------------------------------------- 依赖触发数据接入（幂等）
    def ingest_trigger(self, values: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """接入一条依赖触发数据。

        同一下游作业、同一触发时刻重复上报按幂等处理（重复扫描不重复入账），
        晚到的新时刻会作为该下游的最新触发参与下一次重算并修正预警。
        """
        job_name = str(values.get("下游作业") or "").strip()
        if not job_name:
            return {}, "缺少下游作业，无法登记依赖触发"
        tz_name = values.get("时区")
        triggered = cal.parse_datetime(values.get("触发时间"), tz_name=tz_name)
        expected = cal.parse_datetime(values.get("应到时间"), tz_name=tz_name)
        if triggered is None:
            return {}, "触发时间格式无法识别，支持 2026-11-20 或 2026-11-20T18:00:00+08:00"

        trigger_utc = cal.to_utc(triggered)
        rows = store.rows(TRIGGER_MODULE)
        for row in rows:
            if row.get("下游作业") == job_name and row.get("触发时间UTC") == trigger_utc.isoformat():
                return row, "该下游作业的同一触发已存在，按幂等处理，未重复入账"

        is_critical = self._as_bool(values.get("关键下游标志"), default=True)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "下游作业": job_name,
            "上游作业": str(values.get("上游作业") or "").strip(),
            "触发时间UTC": trigger_utc.isoformat(),
            "触发时间本地": triggered.astimezone(
                cal.parse_timezone(tz_name)).isoformat(),
            "应到时间UTC": cal.to_utc(expected).isoformat() if expected else None,
            "关键下游标志": is_critical,
            "时区": tz_name or cal.DEFAULT_TIMEZONE,
            "到达批次": str(values.get("到达批次") or "").strip(),
        }
        rows.append(entry)
        return entry, "依赖触发已登记，下次重算时按最新时刻修正预警"

    # ------------------------------------------------------------ 主流程：重算
    def recompute(
        self,
        *,
        baseline_id: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """从基线承诺时间与依赖触发数据全量/单基线重算预警结果。

        - 规则口径变更后只重算未完成运行；已完成运行的历史判定冻结保留。
        - 结果按（基线编号, 运行编号）定位并就地更新，不产生重复预警记录。
        - 每次重算递增结果版本，水位变化追加历史判定，便于核对修正轨迹。
        """
        now_utc = cal.to_utc(now) if now else datetime.now(tz=cal.timezone.utc)
        baselines = store.rows(BASELINE_MODULE)
        runtimes = store.rows(RUNTIME_MODULE)
        triggers = store.rows(TRIGGER_MODULE)
        alerts = store.rows(ALERT_MODULE)

        active_baseline_nos = {b.get("基线编号") for b in baselines
                               if b.get("status") in ACTIVE_BASELINE_STATUSES}
        index = {(a.get("基线编号"), a.get("运行编号")): a for a in alerts}
        stats = {"scanned": 0, "updated": 0, "created": 0, "frozen": 0,
                 "notified_once": 0, "level_changes": 0, "retired": 0}

        for baseline in baselines:
            if baseline_id is not None and int(baseline.get("id", 0)) != baseline_id:
                continue
            if baseline.get("status") not in ACTIVE_BASELINE_STATUSES:
                continue

            jobs = self._related_jobs(baseline)
            committed_raw = cal.parse_datetime(baseline.get("承诺完成时间"),
                                               tz_name=cal.DEFAULT_TIMEZONE)
            if committed_raw is None:
                # 脏数据不拖垮整轮重算：跳过并保留该基线的既有预警结果
                continue
            batch = cal.select_batch(committed_raw.date())
            for runtime in runtimes:
                if runtime.get("作业名称") not in jobs:
                    continue
                stats["scanned"] += 1
                key = (baseline.get("基线编号"), runtime.get("记录编号"))
                alert = index.get(key)
                if alert is not None and alert.get("冻结标志"):
                    # 已完成运行：延续既存历史判定，规则换版也不重算
                    stats["frozen"] += 1
                    continue

                result, changed = self._build_alert(
                    baseline, runtime, batch, triggers, now_utc, alert)
                if alert is None:
                    result["id"] = max((int(a.get("id", 0)) for a in alerts), default=0) + 1
                    alerts.append(result)
                    index[key] = result
                    stats["created"] += 1
                else:
                    # 就地更新时保留稳定主键：clear 会把 id 一并清掉，需先取出再回填，
                    # 否则依赖解析侧的「来源预警结果id」会断链。
                    alert_id = alert.get("id")
                    alert.clear()
                    alert.update(result)
                    alert["id"] = alert_id
                    stats["updated"] += 1
                if changed:
                    stats["level_changes"] += 1
                stats["notified_once"] += 1 if result.get("_newly_notified") else 0
                result.pop("_newly_notified", None)

        # 基线停用后其未冻结预警退役（不删除，保留记录与历史）；重新启用时下一轮重算恢复
        for alert in alerts:
            if alert.get("冻结标志"):
                continue
            if alert.get("基线编号") not in active_baseline_nos and alert.get("生效标志", True):
                alert["生效标志"] = False
                stats["retired"] += 1

        return {"ok": True, "message": "基线预警重算完成", "stats": stats,
                "重算时刻UTC": now_utc.isoformat()}

    # -------------------------------------------------------- 单条预警的计算
    def _build_alert(
        self,
        baseline: dict[str, Any],
        runtime: dict[str, Any],
        batch: dict[str, Any] | None,
        triggers: list[dict[str, Any]],
        now_utc: datetime,
        previous: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        tz_name = (batch or {}).get("标准时区") or cal.DEFAULT_TIMEZONE
        committed_local_raw = cal.parse_datetime(baseline.get("承诺完成时间"),
                                                 tz_name=tz_name)
        committed_utc_raw = cal.to_utc(committed_local_raw)

        # 节假日/周末偏移：承诺时刻不在工作日时按基线口径修正，绝不按自然日直接破线
        shift_mode = self._first_text(baseline.get("节假日偏移"),
                                      choices=("顺延", "提前", "不偏移"))
        shifted_local, shift_detail = cal.shift_commitment(
            committed_local_raw, batch or {"标准时区": tz_name},
            shift_mode=shift_mode)
        committed_utc = cal.to_utc(shifted_local)

        margin = self._warn_margin(baseline)
        remaining_minutes = int((committed_utc - now_utc).total_seconds() // 60)
        workday_remaining = cal.workday_minutes_between(now_utc, committed_utc,
                                                        batch or {"标准时区": tz_name})
        # 自然日差仅留作展示与旧口径对照，不参与任何水位判定
        natural_day_gap = (committed_utc.date() - now_utc.date()).days

        level = self._classify(remaining_minutes, margin)

        # 关键下游延迟标注：读依赖触发数据，同一预警结果内同一下游只记账一次
        delay_detail, notified_ledger, newly_notified = self._downstream_delay(
            baseline, triggers, now_utc, previous)
        key_delayed = bool(delay_detail)
        if key_delayed and level == cal.WATER_NORMAL:
            # 关键下游已延迟即使余量尚足，也先抬到「即将破线」预警水位
            level = cal.WATER_WARNING

        run_finished = runtime.get("执行状态") in FINISHED_RUNTIME_STATUSES
        old_level = previous.get("预警水位") if previous else None
        changed = old_level is not None and old_level != level

        history = list(previous.get("历史判定") or []) if previous else []
        if old_level is None or changed:
            history.append({
                "重算版本": (previous.get("重算版本", 0) + 1) if previous else 1,
                "时刻UTC": now_utc.isoformat(),
                "预警水位": level,
                "剩余分钟": remaining_minutes,
                "规则版本": (batch or {}).get("规则版本"),
                "触发原因": "首次生成" if previous is None else f"水位由「{old_level}」修正为「{level}」",
            })

        result = {
            "基线编号": baseline.get("基线编号"),
            "基线名称": baseline.get("基线名称"),
            "运行编号": runtime.get("记录编号"),
            "作业名称": runtime.get("作业名称"),
            "运行状态": runtime.get("执行状态"),
            "承诺时间UTC": committed_utc.isoformat(),
            "承诺时间本地": shifted_local.isoformat(),
            "原始承诺时间UTC": committed_utc_raw.isoformat(),
            "标准时区": tz_name,
            "规则批次id": (batch or {}).get("id"),
            "规则版本": (batch or {}).get("规则版本"),
            "节假日偏移明细": shift_detail,
            "预警余量分钟": margin,
            "剩余分钟": remaining_minutes,
            "工作日余量分钟": workday_remaining,
            "自然日差_仅展示_不参与判定": natural_day_gap,
            "预警水位": level,
            "关键下游延迟": key_delayed,
            "关键下游延迟明细": delay_detail,
            "已通知关键下游": notified_ledger,
            "关键下游延迟次数": len(delay_detail),
            "重算版本": (previous.get("重算版本", 0) + 1) if previous else 1,
            "最近重算时间UTC": now_utc.isoformat(),
            "冻结标志": bool(run_finished),
            "生效标志": True,
            "历史判定": history,
            "_newly_notified": newly_notified,
        }
        return result, changed

    def _classify(self, remaining_minutes: int, margin: int) -> str:
        """三档水位：已过修正后承诺时刻 → 已破线；进入余量窗口 → 即将破线。"""
        if remaining_minutes < 0:
            return cal.WATER_BREACHED
        if remaining_minutes <= margin:
            return cal.WATER_WARNING
        return cal.WATER_NORMAL

    # ------------------------------------------------------------ 关键下游部分
    def _downstream_delay(
        self,
        baseline: dict[str, Any],
        triggers: list[dict[str, Any]],
        now_utc: datetime,
        previous: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        """扫描关联的关键下游，返回延迟明细、通知台账、本次是否有新记账。

        去重口径：台账里已通知过的下游，重复扫描不再产生新预警（只刷新明细）；
        晚到触发使下游恢复后该下游从台账移除，等于结束本次延迟事件，
        下一次延迟才允许再次通知一次。
        """
        key_downstreams = self._key_downstreams(baseline)
        latest: dict[str, dict[str, Any]] = {}
        for trigger in triggers:
            name = trigger.get("下游作业")
            if name not in key_downstreams or not trigger.get("关键下游标志", True):
                continue
            old = latest.get(name)
            # 晚到修正：按到达顺序（id 递增）取该下游最新一条，
            # 而不是按触发时刻——修正数据的触发时刻可能更早。
            if old is None or int(trigger.get("id", 0)) > int(old.get("id", 0)):
                latest[name] = trigger

        ledger = list(previous.get("已通知关键下游") or []) if previous else []
        detail: list[dict[str, Any]] = []
        newly_notified = False
        for name in key_downstreams:
            trigger = latest.get(name)
            delayed, reason, expected_utc = self._judge_delay(name, trigger, now_utc)
            if not delayed:
                continue
            detail.append({
                "下游作业": name,
                "延迟原因": reason,
                "应到时间UTC": expected_utc,
                "最新触发UTC": trigger.get("触发时间UTC") if trigger else None,
            })
            if name not in ledger:
                ledger.append(name)
                newly_notified = True
        # 恢复的下游移出台账，关闭本次延迟事件
        ledger = [name for name in ledger if name in {d["下游作业"] for d in detail}]
        return detail, ledger, newly_notified

    def _judge_delay(
        self,
        name: str,
        trigger: dict[str, Any] | None,
        now_utc: datetime,
    ) -> tuple[bool, str, str | None]:
        if trigger is None:
            return False, "", None
        expected_iso = trigger.get("应到时间UTC")
        if expected_iso:
            expected = cal.parse_datetime(expected_iso, tz_name="UTC")
            triggered = cal.parse_datetime(trigger.get("触发时间UTC"), tz_name="UTC")
            if triggered and triggered > expected:
                return True, "实际触发晚于应到时间", expected_iso
            if triggered is None and now_utc > expected:
                return True, "超过应到时间仍未触发", expected_iso
        return False, "", None

    # --------------------------------------------- 依赖解析侧：共享水位 + 标注
    def annotate_dependencies(self) -> list[dict[str, Any]]:
        """依赖解析清单读取同一份基线预警结果数据，标注关键下游延迟。

        依赖解析模块不持有第二份水位：预警水位、延迟标志全部来自
        ``baseline_alert``，保证两份结果同源、同口径。
        """
        alerts = [a for a in store.rows(ALERT_MODULE) if a.get("生效标志", True)]
        water_by_job: dict[str, dict[str, Any]] = {}
        for alert in alerts:
            job = alert.get("作业名称")
            # 同一作业被多条基线覆盖时取最严水位（已破线 > 即将破线 > 正常）
            rank = {cal.WATER_NORMAL: 0, cal.WATER_WARNING: 1,
                    cal.WATER_BREACHED: 2}
            if job not in water_by_job or rank[alert["预警水位"]] > rank[water_by_job[job]["预警水位"]]:
                water_by_job[job] = alert

        annotated: list[dict[str, Any]] = []
        for dep in store.rows("dependency"):
            row = dict(dep)
            downstream = dep.get("下游作业")
            shared = water_by_job.get(downstream)
            delay_detail = next(
                (d for alert in alerts
                 for d in (alert.get("关键下游延迟明细") or [])
                 if d.get("下游作业") == downstream), None)
            row["共享预警水位"] = shared["预警水位"] if shared else cal.WATER_NORMAL
            row["关键下游延迟"] = bool(delay_detail)
            row["关键下游延迟原因"] = delay_detail["延迟原因"] if delay_detail else ""
            row["来源预警结果id"] = shared["id"] if shared else None
            row["水位口径"] = "基线预警结果数据（工作日/节假日/统一UTC口径）"
            annotated.append(row)
        return annotated

    def timeline_view(self) -> list[dict[str, Any]]:
        """基线时间线：与依赖解析清单读同一份预警水位，供交付核对。"""
        items = []
        for alert in store.rows(ALERT_MODULE):
            if not alert.get("生效标志", True):
                continue
            items.append({
                "预警结果id": alert.get("id"),
                "基线编号": alert.get("基线编号"),
                "作业名称": alert.get("作业名称"),
                "预警水位": alert.get("预警水位"),
                "承诺时间本地": alert.get("承诺时间本地"),
                "剩余分钟": alert.get("剩余分钟"),
                "工作日余量分钟": alert.get("工作日余量分钟"),
                "关键下游延迟": alert.get("关键下游延迟"),
                "规则版本": alert.get("规则版本"),
                "冻结标志": alert.get("冻结标志"),
                "重算版本": alert.get("重算版本"),
            })
        return items

    # -------------------------------------------------------------- 字段解析
    @staticmethod
    def _as_bool(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "是", "y", "yes"):
            return True
        if text in ("false", "0", "否", "n", "no"):
            return False
        return default

    @staticmethod
    def _first_text(value: Any, *, choices: tuple[str, ...]) -> str | None:
        text = str(value or "").strip()
        return text if text in choices else None

    @staticmethod
    def _warn_margin(baseline: dict[str, Any]) -> int:
        try:
            margin = int(float(baseline.get("预警余量分钟")))
            return margin if margin >= 0 else cal.DEFAULT_WARN_MARGIN_MINUTES
        except (TypeError, ValueError):
            # 兼容既有记录：老示例数据该字段是占位文本，回退默认余量
            return cal.DEFAULT_WARN_MARGIN_MINUTES

    @staticmethod
    def _split_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        text = str(value).strip()
        if not text:
            return []
        for sep in ("，", ",", "；", ";", "|"):
            text = text.replace(sep, ",")
        return [part.strip() for part in text.split(",") if part.strip()]

    def _related_jobs(self, baseline: dict[str, Any]) -> set[str]:
        return set(self._split_list(baseline.get("关联作业列表")))

    def _key_downstreams(self, baseline: dict[str, Any]) -> list[str]:
        # 关键下游清单：优先读结构化字段，缺省时关联作业本身即视为关键下游
        explicit = self._split_list(baseline.get("关键下游列表"))
        if explicit:
            return explicit
        return sorted(self._related_jobs(baseline))


baseline_alert_service = BaselineAlertService()
