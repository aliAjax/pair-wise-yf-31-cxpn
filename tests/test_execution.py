import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, iso, utcnow


class ExecutionTrackingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = AirlineRecoveryService(Path(self.tmp.name) / "test.db")
        base = utcnow() + timedelta(days=1)
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(base + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(base + timedelta(days=5))})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "AAA", "duty_start": iso(base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "乙组", "base": "AAA", "duty_start": iso(base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(base - timedelta(days=1)), "valid_to": iso(base + timedelta(days=2))})
        self.base = base

    def tearDown(self): self.tmp.cleanup()

    def make_flight(self, number, aircraft="AC1", crew="CR1", start=None, hours=2):
        start = start or self.base
        return self.svc.create_flight("sched", "scheduler", {"flight_no": number, "origin": "AAA", "destination": "BBB",
            "std": iso(start), "sta": iso(start + timedelta(hours=hours)), "aircraft_id": aircraft, "crew_id": crew, "passenger_count": 150})

    def lock_swap_plan(self, number="AB100"):
        flight = self.make_flight(number)
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1",
            "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=3))})
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "换飞机并延误",
            "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5))}]})
        locked = self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        return flight, locked, disruption

    def item(self, plan):
        self.assertEqual(len(plan["execution_items"]), 1)
        return plan["execution_items"][0]

    def test_lock_generates_pending_items_and_summary(self):
        flight, locked, _ = self.lock_swap_plan()
        item = self.item(locked)
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["notified_at"])
        summary = locked["execution_summary"]
        self.assertEqual(summary["counts"], {"pending": 1, "notified": 0, "executing": 0, "completed": 0, "canceled": 0})
        self.assertEqual(summary["affected_passengers"], 150)

    def test_must_notify_before_execute_or_cancel(self):
        _, locked, _ = self.lock_swap_plan()
        item = self.item(locked)
        with self.assertRaises(ApiError) as ctx:
            self.svc.execute_item(item["id"], "sched", "scheduler")
        self.assertEqual(ctx.exception.code, "crew_not_notified")
        with self.assertRaises(ApiError) as ctx:
            self.svc.cancel_item(item["id"], "sched", "scheduler", {"reason": "测试"})
        self.assertEqual(ctx.exception.code, "crew_not_notified")
        with self.assertRaises(ApiError) as ctx:
            self.svc.notify_item(item["id"], "sched", "scheduler", {"notify_result": "carrier_pigeon"})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self.svc.notify_item(item["id"], "v", "viewer", {"notify_result": "notified"})
        self.assertEqual(ctx.exception.status, 403)

    def test_notify_execute_complete_flow(self):
        _, locked, _ = self.lock_swap_plan()
        item = self.item(locked)
        notified = self.svc.notify_item(item["id"], "sched", "scheduler", {"notify_result": "notified", "notify_detail": "机组确认"})
        item = self.item(notified)
        self.assertEqual(item["status"], "notified")
        self.assertEqual(item["notified_by"], "sched")
        self.assertEqual(item["notify_detail"], "机组确认")
        executing = self.svc.execute_item(item["id"], "sched", "scheduler")
        self.assertEqual(self.item(executing)["status"], "executing")
        # 执行中的资源继续占用：不能完成未执行项之外的跳转，执行中不能再次执行
        with self.assertRaises(ApiError) as ctx:
            self.svc.execute_item(item["id"], "sched", "scheduler")
        self.assertEqual(ctx.exception.code, "item_state")
        completed = self.svc.complete_item(item["id"], "sched", "scheduler")
        item = self.item(completed)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["completed_by"], "sched")
        with self.assertRaises(ApiError) as ctx:
            self.svc.cancel_item(item["id"], "sched", "scheduler", {"reason": "想取消"})
        self.assertEqual(ctx.exception.code, "item_completed")
        self.assertEqual(completed["execution_summary"]["counts"]["completed"], 1)

    def test_executing_resource_blocks_new_plan_but_cancel_releases_it(self):
        flight, locked, disruption = self.lock_swap_plan()
        item = self.item(locked)
        self.svc.notify_item(item["id"], "sched", "scheduler", {"notify_result": "notified"})
        self.svc.execute_item(item["id"], "sched", "scheduler")
        # 新航班想占用 AC2/CR2 的同一时段：执行中资源继续占用，锁定被拒
        other = self.make_flight("AB102", "AC2", "CR2", start=self.base + timedelta(hours=4), hours=2)
        plan2 = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "接手方案",
            "assignments": [{"flight_id": other["id"], "aircraft_id": "AC2", "crew_id": "CR2",
            "new_std": iso(self.base + timedelta(hours=4)), "new_sta": iso(self.base + timedelta(hours=6))}]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(plan2["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "locked_resource_conflict")
        # 临时取消原项，注明原因和操作人
        canceled = self.svc.cancel_item(item["id"], "sched", "scheduler", {"reason": "天气备降，机组超时"})
        item = self.item(canceled)
        self.assertEqual(item["status"], "canceled")
        self.assertEqual(item["cancel_reason"], "天气备降，机组超时")
        self.assertEqual(item["canceled_by"], "sched")
        self.assertTrue(item["canceled_at"])
        stored_flight = self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (flight["id"],)).fetchone()
        self.assertEqual(stored_flight["status"], "canceled")
        self.assertEqual(stored_flight["cancel_reason"], "天气备降，机组超时")
        assignment = self.svc.repo.conn.execute("SELECT status FROM assignments WHERE id=?", (item["assignment_id"],)).fetchone()
        self.assertEqual(assignment["status"], "canceled")
        self.assertEqual(canceled["execution_summary"]["counts"]["canceled"], 1)
        # 资源已从原时段释放，新方案可以接手并锁定
        relocked = self.svc.lock_plan(plan2["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(relocked["status"], "locked")

    def test_cancel_requires_reason(self):
        _, locked, _ = self.lock_swap_plan()
        item = self.item(locked)
        self.svc.notify_item(item["id"], "sched", "scheduler", {"notify_result": "no_answer"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.cancel_item(item["id"], "sched", "scheduler", {"reason": "  "})
        self.assertEqual(ctx.exception.code, "reason_required")

    def test_plan_level_canceled_assignment_tracks_and_cancels_flight(self):
        flight = self.make_flight("AB200", start=self.base + timedelta(hours=6))
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "airport_closure", "resource_id": "AAA",
            "starts_at": iso(self.base + timedelta(hours=5)), "ends_at": iso(self.base + timedelta(hours=8))})
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "取消该航班",
            "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC1", "crew_id": "CR1", "status": "canceled",
            "new_std": iso(self.base + timedelta(hours=6)), "new_sta": iso(self.base + timedelta(hours=8))}]})
        locked = self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        item = self.item(locked)
        self.assertEqual(item["status"], "pending")
        with self.assertRaises(ApiError) as ctx:
            self.svc.execute_item(item["id"], "sched", "scheduler")
        self.assertEqual(ctx.exception.code, "item_canceled")
        notified = self.svc.notify_item(item["id"], "sched", "scheduler", {"notify_result": "notified"})
        done = self.svc.cancel_item(item["id"], "ops", "ops_manager", {"reason": "机场关闭无法执飞"})
        self.assertEqual(self.item(done)["status"], "canceled")
        stored_flight = self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (flight["id"],)).fetchone()
        self.assertEqual(stored_flight["status"], "canceled")
        self.assertEqual(stored_flight["cancel_reason"], "机场关闭无法执飞")


if __name__ == "__main__": unittest.main()
