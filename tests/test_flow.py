import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class DroneFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="D100", route=None, risk=1, altitude=100):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": "M400", "payload_kg": 5, "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": altitude, "population_risk": risk, "emergency_plan": "返回起降点", "region": "BJ"})

    def test_full_approval_change_and_offline_reconciliation(self):
        plan = self.plan(); submitted = self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})["plan"]
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertTrue(check["approvable"])
        approved = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": submitted["revision"], "offline_id": "offline-1", "reason": "路线和应急方案满足要求"})
        self.assertEqual(approved["plan"]["status"], "approved")
        duplicate = self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": submitted["revision"], "offline_id": "offline-1", "reason": "补传"})
        self.assertTrue(duplicate["idempotent"])
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": submitted["revision"], "route": [[116.12, 39.82], [116.32, 39.92]]})
        self.assertEqual(changed["status"], "draft"); self.assertEqual(changed["revision"], 2)
        notifications = self.svc.notifications("op-user", "operator", "OP1")["notifications"]
        self.assertEqual(notifications[0]["kind"], "approval_invalidated")

    def test_restriction_emergency_override_and_conflicts(self):
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "临时禁飞", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.2, "max_lat": 40.0, "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "活动"})
        plan = self.plan("D101"); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        check = self.svc.check_conflicts(plan["id"], "airspace_reviewer", "")
        self.assertFalse(check["approvable"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "offline-2", "reason": "常规审核"})
        self.assertEqual(ctx.exception.code, "airspace_conflict")
        override = self.svc.approve(plan["id"], "commander", "commander", {"expected_revision": 1, "offline_id": "offline-3", "reason": "紧急任务", "override_reason": "应急救援授权"})
        self.assertEqual(override["plan"]["status"], "approved")
        conflicting = self.plan("D102", route=[[116.11, 39.81], [116.15, 39.84]])
        self.svc.submit(conflicting["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(conflicting["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "offline-4", "reason": "复核"})
        self.assertIn(ctx.exception.code, {"hard_constraint_violation", "airspace_conflict"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.approve(conflicting["id"], "reviewer", "airspace_reviewer", {"expected_revision": 99, "offline_id": "offline-5", "reason": "过期审核"})
        self.assertEqual(ctx.exception.code, "revision_conflict")
    def test_investigation_freeze_blocks_writes_and_release_restores(self):
        plan = self.plan("D110"); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-110", "reason": "通过"})
        inv = self.svc.open_investigation(plan["id"], "cmd", "commander", {"incident_type": "坠机", "discovered_at": iso(utcnow()), "reason": "事故待查"})
        self.assertEqual(inv["status"], "open")
        snap = inv["snapshot"]
        self.assertEqual(snap["plan"]["status"], "approved")
        self.assertEqual(len(snap["approvals"]), 1)
        self.assertIn("conflict_report", snap); self.assertIn("active_restrictions", snap)
        self.assertEqual(inv["snapshot_summary"]["plan_status"], "approved")
        for mutate in (
            lambda: self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "route": [[116.1, 39.8], [116.2, 39.9]]}),
            lambda: self.svc.cancel(plan["id"], "op-user", "operator", "OP1", {"reason": "取消"}),
            lambda: self.svc.submit(plan["id"], "op-user", "operator", "OP1", {}),
            lambda: self.svc.reject(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-111", "reason": "驳回"}),
        ):
            with self.assertRaises(ApiError) as ctx: mutate()
            self.assertEqual(ctx.exception.status, 409); self.assertEqual(ctx.exception.code, "plan_frozen")
        with self.assertRaises(ApiError) as ctx:
            self.svc.open_investigation(plan["id"], "cmd", "commander", {"incident_type": "重复", "discovered_at": iso(utcnow()), "reason": "重复开单"})
        self.assertEqual(ctx.exception.code, "plan_frozen")
        view = self.svc.get_plan(plan["id"], "operator", "OP1")  # 封存期间运营方仍可查看
        self.assertEqual(view["status"], "approved")
        listing = self.svc.list_investigations("operator", "OP1")["investigations"]
        self.assertEqual(len(listing), 1); self.assertEqual(listing[0]["reason"], "事故待查")
        with self.assertRaises(ApiError) as ctx:
            self.svc.close_investigation(inv["id"], "other-cmd", "commander", {"conclusion": "非原开单人"})
        self.assertEqual(ctx.exception.code, "not_opener")
        with self.assertRaises(ApiError) as ctx:
            self.svc.close_investigation(inv["id"], "cmd", "commander", {})
        self.assertEqual(ctx.exception.code, "conclusion_required")
        closed = self.svc.close_investigation(inv["id"], "cmd", "commander", {"conclusion": "操作失误，无新限制"})
        self.assertFalse(closed["approval_invalidated"])
        self.assertEqual(self.svc.get_plan(plan["id"], "commander")["status"], "approved")

    def test_investigation_release_with_new_no_fly_invalidates_approval(self):
        plan = self.plan("D120"); self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-120", "reason": "通过"})
        inv = self.svc.open_investigation(plan["id"], "cmd", "commander", {"incident_type": "失联", "discovered_at": iso(utcnow()), "reason": "事故调查"})
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "新禁飞区", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.4, "max_lat": 40.0, "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=30)), "ends_at": iso(self.start + timedelta(hours=2)), "reason": "安保"})
        closed = self.svc.close_investigation(inv["id"], "cmd", "commander", {"conclusion": "需复飞评估"})
        self.assertTrue(closed["approval_invalidated"])
        self.assertEqual(len(closed["new_no_fly"]), 1)
        self.assertEqual(self.svc.get_plan(plan["id"], "commander")["status"], "draft")
        kinds = [n["kind"] for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"]]
        self.assertIn("approval_invalidated", kinds)


if __name__ == "__main__": unittest.main()
