import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class InvestigationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db"); self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="I200", route=None, risk=1, altitude=100):
        return self.svc.create_plan("op-user", "operator", "OP1", {"callsign": callsign, "drone_model": "M400", "payload_kg": 5, "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "max_altitude": altitude, "population_risk": risk, "emergency_plan": "返回起降点", "region": "BJ"})

    def approve_plan(self, plan, offline="off-i", reviewer="reviewer", role="airspace_reviewer", reason="合规"):
        self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})
        return self.svc.approve(plan["id"], reviewer, role, {"expected_revision": 1, "offline_id": offline, "reason": reason})

    def test_commander_seals_and_snapshots_are_fixed(self):
        plan = self.plan(); self.approve_plan(plan)
        inv = self.svc.open_investigation(plan["id"], "cmdr", "commander", {"accident_type": "collision", "found_at": iso(utcnow() - timedelta(minutes=10)), "freeze_reason": "等待残骸取证"})
        self.assertEqual(inv["status"], "sealed")
        self.assertEqual(inv["accident_type"], "collision")
        summary = inv["snapshot_summary"]
        self.assertEqual(summary["plan"]["revision"], 1)
        self.assertEqual(summary["plan"]["status"], "approved")
        self.assertEqual(summary["reviews"]["approved"], 1)
        self.assertGreaterEqual(summary["notifications"]["count"], 1)
        self.assertIn("blocking_conflicts", summary["restrictions"])
        # 重复封存返回 409
        with self.assertRaises(ApiError) as ctx:
            self.svc.open_investigation(plan["id"], "cmdr", "commander", {"accident_type": "collision", "found_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "investigation_exists")
        # 非指挥官不能开单
        with self.assertRaises(ApiError) as ctx:
            self.svc.open_investigation(plan["id"], "reviewer", "airspace_reviewer", {"accident_type": "collision", "found_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "investigation_forbidden")
        # 未来发现时间被拒绝
        with self.assertRaises(ApiError) as ctx:
            self.svc.open_investigation(self.plan("I201")["id"], "cmdr", "commander", {"accident_type": "x", "found_at": iso(utcnow() + timedelta(hours=5))})
        self.assertEqual(ctx.exception.code, "invalid_found_at")

    def test_sealed_plan_is_read_only_all_mutations_409(self):
        plan = self.plan(); self.approve_plan(plan, offline="off-ro")
        self.svc.open_investigation(plan["id"], "cmdr", "commander", {"accident_type": "loss", "found_at": iso(utcnow())})
        mutation_calls = [
            lambda: self.svc.submit(plan["id"], "op-user", "operator", "OP1", {}),
            lambda: self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "region": "SH"}),
            lambda: self.svc.cancel(plan["id"], "op-user", "operator", "OP1", {"reason": "撤"}),
            lambda: self.svc.approve(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-new", "reason": "封存中补审"}),
            lambda: self.svc.reject(plan["id"], "reviewer", "airspace_reviewer", {"expected_revision": 1, "offline_id": "off-rej", "reason": "封存中拒绝"}),
            lambda: self.svc.cancel(plan["id"], "cmdr", "commander", "", {"reason": "指挥官取消"}),
        ]
        for call in mutation_calls:
            with self.assertRaises(ApiError) as ctx: call()
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(ctx.exception.code, "plan_sealed")
            self.assertEqual(ctx.exception.details["investigation_id"], 1)
        # 查看仍然允许
        viewed = self.svc.get_plan(plan["id"], "op-user", "OP1")
        self.assertEqual(viewed["sealed_investigation_id"], 1)
        self.assertEqual(viewed["status"], "approved")
        # 将计划时间回拨到已到期：到期处理仍需跳过封存计划
        past = iso(utcnow() - timedelta(hours=1))
        self.svc.repo.conn.execute("UPDATE flight_plans SET starts_at=?, ends_at=? WHERE id=?", (past, past, plan["id"]))
        expired = self.svc.expire_plans("reviewer", "airspace_reviewer")
        self.assertEqual(expired["sealed_skipped"], 1)
        self.assertEqual(expired["expired"], 0)

    def test_release_by_other_commander_rejected(self):
        plan = self.plan(); self.approve_plan(plan, offline="off-owner")
        self.svc.open_investigation(plan["id"], "cmdr-1", "commander", {"accident_type": "collision", "found_at": iso(utcnow())})
        with self.assertRaises(ApiError) as ctx:
            self.svc.release_investigation(1, "cmdr-2", "commander", {"conclusion": "非开单人"})
        self.assertEqual(ctx.exception.code, "investigation_owner_only")
        with self.assertRaises(ApiError) as ctx:
            self.svc.release_investigation(1, "reviewer", "airspace_reviewer", {"conclusion": "审核员越权"})
        self.assertEqual(ctx.exception.code, "investigation_forbidden")
        with self.assertRaises(ApiError) as ctx:
            self.svc.release_investigation(999, "cmdr-1", "commander", {"conclusion": "不存在"})
        self.assertEqual(ctx.exception.code, "investigation_not_found")
        with self.assertRaises(ApiError) as ctx:
            self.svc.release_investigation(1, "cmdr-1", "commander", {"conclusion": ""})
        self.assertEqual(ctx.exception.code, "conclusion_required")

    def test_release_keeps_approval_when_no_new_no_fly(self):
        plan = self.plan(); self.approve_plan(plan, offline="off-keep")
        self.svc.open_investigation(plan["id"], "cmdr", "commander", {"accident_type": "minor", "found_at": iso(utcnow())})
        out = self.svc.release_investigation(1, "cmdr", "commander", {"conclusion": "原因查明，无需停飞"})
        self.assertEqual(out["status"], "released")
        self.assertEqual(self.svc.get_plan(plan["id"], "commander")["status"], "approved")
        # 再次解除 409
        with self.assertRaises(ApiError) as ctx:
            self.svc.release_investigation(1, "cmdr", "commander", {"conclusion": "重复"})
        self.assertEqual(ctx.exception.code, "investigation_closed")
        # 解封后变更恢复可用
        changed = self.svc.change(plan["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "region": "TJ"})
        self.assertEqual(changed["status"], "draft")

    def test_new_no_fly_zone_voids_approval_on_release(self):
        plan = self.plan(route=[[116.1, 39.8], [116.3, 39.9]])
        self.approve_plan(plan, offline="off-nfz")
        self.svc.open_investigation(plan["id"], "cmdr", "commander", {"accident_type": "collision", "found_at": iso(utcnow())})
        # 封存期间出现与计划时空重叠的新禁飞区
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "事故后新增禁飞", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.4, "max_lat": 40.0, "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=5)), "ends_at": iso(self.start + timedelta(hours=3)), "reason": "事故区域管控"})
        out = self.svc.release_investigation(1, "cmdr", "commander", {"conclusion": "区域继续管控"})
        self.assertEqual(out["status"], "released")
        self.assertEqual(self.svc.get_plan(plan["id"], "commander")["status"], "draft")
        notifications = self.svc.notifications("op-user", "operator", "OP1")["notifications"]
        kinds = {n["kind"] for n in notifications}
        self.assertIn("approval_invalidated", kinds); self.assertIn("investigation_released", kinds)

    def test_preexisting_restriction_in_snapshot_does_not_void(self):
        # 封存前已存在并记录在快照中的禁飞区，不应在解除时判定为"新"禁飞区
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "旧禁飞", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.4, "max_lat": 40.0, "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(hours=1)), "ends_at": iso(self.start + timedelta(hours=3)), "reason": "原有管控"})
        plan = self.plan()
        # 旧禁飞区阻断常规批准，由指挥官紧急授权覆盖
        submitted = self.svc.submit(plan["id"], "op-user", "operator", "OP1", {})["plan"]
        self.svc.approve(plan["id"], "cmdr", "commander", {"expected_revision": submitted["revision"], "offline_id": "off-old", "reason": "紧急", "override_reason": "救援"})
        inv = self.svc.open_investigation(plan["id"], "cmdr", "commander", {"accident_type": "collision", "found_at": iso(utcnow())})
        self.assertEqual(inv["snapshot_summary"]["restrictions"]["active_count"], 1)
        out = self.svc.release_investigation(inv["id"], "cmdr", "commander", {"conclusion": "解除"})
        self.assertEqual(self.svc.get_plan(plan["id"], "commander")["status"], "approved")
        self.assertEqual(out["status"], "released")

    def test_unrelated_new_no_fly_does_not_void(self):
        plan = self.plan(); self.approve_plan(plan, offline="off-far")
        self.svc.open_investigation(plan["id"], "cmdr", "commander", {"accident_type": "minor", "found_at": iso(utcnow())})
        # 与计划空域不重叠的新禁飞区
        self.svc.create_restriction("reviewer", "airspace_reviewer", {"name": "远端禁飞", "kind": "no_fly", "min_lon": 100.0, "min_lat": 20.0, "max_lon": 100.5, "max_lat": 20.5, "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=1)), "reason": "他处活动"})
        self.svc.release_investigation(1, "cmdr", "commander", {"conclusion": "无关联"})
        self.assertEqual(self.svc.get_plan(plan["id"], "commander")["status"], "approved")

    def test_investigation_list_visibility_and_payload(self):
        plan = self.plan(); self.approve_plan(plan, offline="off-list")
        self.svc.open_investigation(plan["id"], "cmdr", "commander", {"accident_type": "collision", "freeze_reason": "冻结取证", "found_at": iso(utcnow())})
        staff = self.svc.list_investigations("cmdr", "commander", "")["investigations"]
        self.assertEqual(len(staff), 1)
        self.assertEqual(staff[0]["freeze_reason"], "冻结取证")
        self.assertIn("snapshot_summary", staff[0])
        # 运营方只能看到自己运营方的调查
        own = self.svc.list_investigations("op-user", "operator", "OP1")["investigations"]
        self.assertEqual(len(own), 1)
        other = self.svc.list_investigations("op-user-2", "operator", "OP2")["investigations"]
        self.assertEqual(other, [])
        # viewer 不允许看调查清单
        with self.assertRaises(ApiError) as ctx:
            self.svc.list_investigations("v", "viewer", "")
        self.assertEqual(ctx.exception.code, "investigations_forbidden")
        # 详情访问控制
        self.svc.get_investigation(1, "op-user", "operator", "OP1")
        with self.assertRaises(ApiError) as ctx:
            self.svc.get_investigation(1, "op-user-2", "operator", "OP2")
        self.assertEqual(ctx.exception.code, "investigation_forbidden")


if __name__ == "__main__": unittest.main()
