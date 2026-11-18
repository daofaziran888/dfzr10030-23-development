"""依赖任务与调度状态绑定的规则测试。

直接驱动服务层，不依赖 HTTP 或浏览器：
    cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services import dependency
from app.services.schedule import ScheduleService
from app.store import Store, store


def fresh_store() -> Store:
    """每个用例重建仓库，避免用例之间互相影响。"""
    store._tables = {name: [dict(row) for row in rows] for name, rows in _seed_rows().items()}
    store._aux = {name: [dict(row) for row in rows] for name, rows in _seed_aux().items()}
    return store


def _seed_rows():
    from app.seed import SEED_ROWS
    return SEED_ROWS


def _seed_aux():
    from app.seed import SEED_AUX
    return SEED_AUX


class DependencyGateTest(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store()
        self.service = ScheduleService()

    def test_unfinished_dependency_blocks_start(self) -> None:
        self.service.run_action(1, "确认排期")
        entry, message = self.service.run_action(1, "启动调度")
        self.assertIsNone(entry)
        self.assertIn("未完成", message)
        plan = store.find("schedule", 1)
        self.assertEqual(plan["status"], "已排期")
        self.assertEqual(plan["阶段"], "等待依赖")

    def test_waiting_list_tracks_plan(self) -> None:
        self.service.run_action(1, "确认排期")
        waiting = self.service.waiting_list()
        self.assertTrue(any(item["计划编号"] == "SCHE-0001" for item in waiting))
        item = next(item for item in waiting if item["计划编号"] == "SCHE-0001")
        self.assertEqual(item["依赖任务"], "ROBO-0002")
        self.assertTrue(item["缺失分区"].startswith("ROBO-0002/"))

    def test_batch_requires_dependency_evidence(self) -> None:
        self.service.run_action(1, "确认排期")
        entry, message = self.service.generate_batch(1)
        self.assertIsNone(entry)
        self.assertTrue("运行中" in message or "依赖完成" in message, message)
        self.assertEqual(self.service.batches(1), [])

    def test_running_plan_with_unfinished_dependency_is_conflict(self) -> None:
        plan = store.find("schedule", 3)
        task = next(row for row in store.rows("robot_task") if row["任务编号"] == "ROBO-0003")
        task["status"] = "执行中"
        # 让回执失效：把已完成回执改成未完成
        for receipt in store.aux_rows(dependency.RECEIPT_MODULE):
            if receipt["任务编号"] == "ROBO-0003":
                receipt["完成状态"] = "未完成"
        snapshot = dependency.ensure_plan(plan)
        self.assertEqual(snapshot["阶段"], "运行中冲突")
        verdict = self.service.dependency_status(3)
        self.assertEqual(verdict["阶段"], "运行中冲突")


class DelayBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store()
        self.service = ScheduleService()

    def test_two_boundaries_have_same_batch_guarantee(self) -> None:
        comparison = self.service.compare_options(2)
        options = {item["选项"]: item for item in comparison["选项"]}
        self.assertEqual(options["停在已排期"]["阶段"], "等待依赖")
        self.assertEqual(options["进入暂运行"]["阶段"], "暂运行待回执")
        self.assertFalse(options["停在已排期"]["是否产出运行批次"])
        self.assertFalse(options["进入暂运行"]["是否产出运行批次"])
        self.assertFalse(options["停在已排期"]["是否读取依赖回执"])
        self.assertTrue(options["进入暂运行"]["是否读取依赖回执"])

    def test_compare_does_not_mutate(self) -> None:
        before = dict(store.find("schedule", 2))
        count = len(store.aux_rows(dependency.RUN_PLAN_MODULE))
        self.service.compare_options(2)
        self.assertEqual(count, len(store.aux_rows(dependency.RUN_PLAN_MODULE)))
        self.assertEqual(before["status"], store.find("schedule", 2)["status"])

    def test_delayed_receipt_advances_only_to_ready(self) -> None:
        self.service.run_action(1, "确认排期")
        detail, _ = self.service.report_receipt(
            {"任务编号": "ROBO-0002", "完成状态": "延迟完成", "依赖分区": "ROBO-0002/dt=2026-11-20"})
        self.assertIsNotNone(detail)
        plan = store.find("schedule", 1)
        self.assertEqual(plan["status"], "已排期")
        self.assertEqual(plan["阶段"], "已就绪")
        # 依赖完成后必须再推进一次才进入运行
        entry, _ = self.service.run_action(1, "启动调度")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["阶段"], "运行中")


class SnapshotHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store()
        self.service = ScheduleService()

    def test_recalculation_appends_and_keeps_history(self) -> None:
        self.service.run_action(1, "确认排期")
        self.service.run_plans()
        first = [row for row in self.service.run_plans(include_history=True) if row["plan_id"] == 1]
        self.assertEqual(len(first), 1)
        version_one = dict(first[0])
        self.service.report_receipt(
            {"任务编号": "ROBO-0002", "完成状态": "延迟完成", "依赖分区": "ROBO-0002/dt=2026-11-20"})
        history = [row for row in self.service.run_plans(include_history=True) if row["plan_id"] == 1]
        self.assertGreater(len(history), 1)
        preserved = next(row for row in history if row["快照版本"] == version_one["快照版本"])
        self.assertEqual(preserved["阶段"], version_one["阶段"])
        self.assertEqual(preserved["依赖完成状态"], version_one["依赖完成状态"])

    def test_recalculation_scope_is_respected(self) -> None:
        # 计划 1 的适用范围是「仅本计划」；另建一条同依赖但范围也是「仅本计划」的计划，
        # 显式重算计划 1 时不应把另一条也推进。
        other = self.service.create_entry({
            "计划编号": "SCHE-9000", "关联任务": "ROBO-0000", "调度周期": "每日",
            "开始时间": "2026-11-01", "依赖任务": "ROBO-0002", "延迟策略": "停在已排期",
            "适用范围": "仅本计划",
        })[0]
        self.assertIsNotNone(other)
        self.service.run_action(other["id"], "确认排期")
        own_before = len([r for r in self.service.run_plans(include_history=True)
                          if r["plan_id"] == other["id"]])
        outcome, _ = self.service.recalculate(1)
        self.assertEqual(outcome["scope"], "仅本计划")
        self.assertEqual([item["plan"]["id"] for item in outcome["plans"]], [1])
        own_after = [r for r in self.service.run_plans(include_history=True)
                     if r["plan_id"] == other["id"]]
        self.assertEqual(len(own_after), own_before)
        self.assertEqual(other["阶段"], "等待依赖")

    def test_shared_dependency_scope_expands_recalculation(self) -> None:
        sibling = self.service.create_entry({
            "计划编号": "SCHE-9001", "关联任务": "ROBO-0000", "调度周期": "每日",
            "开始时间": "2026-11-01", "依赖任务": "ROBO-0002", "延迟策略": "停在已排期",
            "适用范围": "同依赖任务",
        })[0]
        self.service.run_action(sibling["id"], "确认排期")
        outcome, _ = self.service.recalculate(sibling["id"])
        affected = {item["plan"]["计划编号"] for item in outcome["plans"]}
        # 计划 2 也引用 ROBO-0002 且声明「同依赖任务」，应一起重算；
        # 计划 1 的范围是「仅本计划」，不参与别人的重算。
        self.assertIn("SCHE-9001", affected)
        self.assertIn("SCHE-0002", affected)
        self.assertNotIn("SCHE-0001", affected)
        # 范围外的计划 1 保持草稿，未被推进
        self.assertEqual(store.find("schedule", 1)["status"], "草稿")

    def test_terminal_plans_are_not_recalculated_awake(self) -> None:
        self.service.run_action(3, "暂停调度")
        plan = store.find("schedule", 3)
        self.assertEqual(plan["status"], "已暂停")
        snapshot = dependency.ensure_plan(plan)
        self.assertEqual(snapshot["阶段"], "已暂停")


class ArchiveBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store()
        self.service = ScheduleService()

    def test_archive_requires_terminal_state(self) -> None:
        entry, message = self.service.archive(1)
        self.assertIsNone(entry)
        self.assertIn("已完成或已暂停", message)

    def test_restore_only_for_paused(self) -> None:
        self.service.run_action(2, "暂停调度")
        entry, _ = self.service.archive(2)
        self.assertIsNotNone(entry)
        # 归档后计划不再出现在运行计划视图，但历史视图仍可审计
        self.assertEqual(self.service.run_plans(plan_id=2), [])
        self.assertTrue([row for row in self.service.run_plans(include_history=True, plan_id=2)
                         if row["archived"]])
        restored, _ = self.service.restore(2)
        self.assertIsNotNone(restored)
        self.assertEqual(restored["status"], "已暂停")
        self.assertTrue(self.service.run_plans(plan_id=2))


class BatchIdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store()
        self.service = ScheduleService()

    def test_batch_generated_once_per_partition(self) -> None:
        self.service.report_receipt(
            {"任务编号": "ROBO-0003", "完成状态": "已完成", "依赖分区": "ROBO-0003/dt=2026-11-15"})
        # 计划 3 已在运行中且依赖已完成
        entry, message = self.service.generate_batch(3)
        self.assertIsNotNone(entry, message)
        again, message = self.service.generate_batch(3)
        self.assertIsNone(again)
        self.assertIn("重复", message)
        self.assertEqual(len(self.service.batches(3)), 1)


class LogConsumptionTest(unittest.TestCase):
    def setUp(self) -> None:
        fresh_store()
        from app.services.log_viewer import LogViewerService
        self.service = LogViewerService()
        ScheduleService().run_action(1, "确认排期")

    def test_waiting_log_sync_is_idempotent(self) -> None:
        first = self.service.sync_waiting_logs()
        second = self.service.sync_waiting_logs()
        self.assertEqual(len(first["created"]), 2)
        self.assertEqual(len(second["created"]), 0)
        waiting = self.service.waiting_items()
        self.assertTrue(all(item["关联任务日志编号"] for item in waiting))

    def test_workbench_sync_is_idempotent(self) -> None:
        first = self.service.sync_workbench()
        second = self.service.sync_workbench()
        self.assertEqual(len(first["created"]), 2)
        self.assertEqual(len(second["created"]), 0)
        self.assertTrue(all(row["审核状态"] == "待审核" for row in self.service.workbench()))

    def test_verify_reports_missing_log(self) -> None:
        result = self.service.verify()
        target = next(item for item in result["items"] if item["计划编号"] == "SCHE-0001")
        self.assertEqual(target["核对状态"], "缺日志")
        self.service.sync_waiting_logs()
        result = self.service.verify()
        target = next(item for item in result["items"] if item["计划编号"] == "SCHE-0001")
        self.assertEqual(target["核对状态"], "待核对")


if __name__ == "__main__":
    unittest.main()
