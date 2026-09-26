"""Decision layer: flight-plan workflow and post-accident investigation sealing."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from kernel import (
    INVESTIGATION_STAFF,
    ROLES,
    ApiError,
    boxes_overlap,
    iso,
    parse_time,
    route_bbox,
    times_overlap,
    utcnow,
    validate_route,
)
from store import Repository


class DroneAirspaceService:
    def __init__(self, path: str): self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    # ------------------------------------------------------------------ plans

    def create_restriction(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        name, kind, reason = str(body.get("name", "")).strip(), str(body.get("kind", "")).strip(), str(body.get("reason", "")).strip()
        if kind not in {"no_fly", "temporary_limit"} or not name or not reason: raise ApiError(400, "invalid_restriction", "名称、类型和原因必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_restriction", "空域范围和高度必须为数字")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or end <= start:
            raise ApiError(400, "invalid_restriction", "空域范围、高度或时间无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO restrictions(name,kind,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,starts_at,ends_at,reason,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, iso()))
            return dict(conn.execute("SELECT * FROM restrictions WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_plan(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "plan_forbidden", "只有运营方可以创建飞行计划")
        required = ("callsign", "drone_model", "starts_at", "ends_at", "emergency_plan", "region")
        if any(body.get(key) in (None, "") for key in required): raise ApiError(400, "missing_fields", "飞行计划字段不完整")
        route = validate_route(body.get("route")); start, end = parse_time(body["starts_at"]), parse_time(body["ends_at"])
        try: payload, altitude = float(body.get("payload_kg")), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_numbers", "payload_kg 和 max_altitude 必须为数字")
        risk = body.get("population_risk")
        if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5:
            raise ApiError(400, "invalid_plan", "载荷、高度或人口风险无效")
        if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "飞行时间必须在未来且结束晚于开始")
        bbox = route_bbox(route)
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("""INSERT INTO flight_plans(operator_id,callsign,drone_model,payload_kg,route_json,starts_at,ends_at,max_altitude,population_risk,emergency_plan,region,created_by,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (operator, str(body["callsign"]).upper(), body["drone_model"], payload, json.dumps(route), iso(start), iso(end), altitude, risk, body["emergency_plan"], body["region"], actor, iso(), iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "plan_duplicate", "同一运营方、呼号和起飞时间的计划已存在") from exc
            plan_id = cur.lastrowid; Repository.audit(conn, plan_id, actor, role, "plan_created", {"bbox": bbox, "revision": 1})
            return self.get_plan(plan_id, role, operator)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    def _active_seal(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM investigations WHERE plan_id=? AND status='sealed' ORDER BY id DESC", (plan_id,)).fetchone()

    def _require_unsealed(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> None:
        """封存期间只允许查看：任何状态改写一律 409。"""
        seal = self._active_seal(conn, plan["id"])
        if seal:
            raise ApiError(409, "plan_sealed",
                           f"计划处于事故调查封存中（调查编号 {seal['id']}），封存期间仅可查看",
                           {"investigation_id": seal["id"], "accident_type": seal["accident_type"], "freeze_reason": seal["freeze_reason"]})

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        route = self._route(plan); bbox = route_bbox(route); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            altitude_overlap = plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0
            if altitude_overlap:
                item = {"code": "airspace_restriction", "restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]}
                blocking.append(item)
        adjacent: list[dict[str, Any]] = []
        for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
            if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002):
                adjacent.append({"plan_id": other["id"], "callsign": other["callsign"], "operator_id": other["operator_id"], "status": other["status"], "starts_at": other["starts_at"], "ends_at": other["ends_at"]})
        if adjacent: blocking.append({"code": "adjacent_traffic", "plans": adjacent, "message": "相邻航路与有效计划重叠"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "hard_violations": hard, "blocking_conflicts": blocking, "approvable": not hard and not blocking}

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        seal = self._active_seal(conn, plan_id)
        if seal: result["sealed_investigation_id"] = seal["id"]
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "valid_until" if "valid_until" in result else "updated_at")}
        if role in {"airspace_reviewer", "commander", "auditor"}: result["approvals"] = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
        return result

    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            self._require_unsealed(conn, plan)
            if plan["status"] == "submitted": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            self._require_unsealed(conn, plan)
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "approved":
                    return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            if plan["status"] == "approved": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以批准")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，审核决定不能套用")
            report = self._conflict_report(conn, plan)
            if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束", report)
            if report["blocking_conflicts"] and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
            override_kind = "emergency_authority" if report["blocking_conflicts"] else None
            cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,created_at)
                                  VALUES(?,?,?,?,?,?,?,?)""", (plan_id, expected, actor, "approved", reason, offline_id, override_kind, iso()))
            conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
            if override_kind: Repository.audit(conn, plan_id, actor, role, "emergency_override_used", {"override_reason": override, "conflicts": report["blocking_conflicts"]})
            Repository.audit(conn, plan_id, actor, role, "plan_approved", {"revision": expected, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "approved", f"飞行计划 {plan['callsign']} 已批准")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False, "approval_id": cur.lastrowid, "override_kind": override_kind}

    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "当前角色不能拒绝计划")
        expected, offline_id, reason = body.get("expected_revision"), str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            self._require_unsealed(conn, plan)
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "rejected": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经被使用")
            if plan["status"] != "submitted" or plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划状态或版本不匹配")
            conn.execute("INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, expected, actor, "rejected", reason, offline_id, iso()))
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_rejected", {"reason": reason, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False}

    def change(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "change_forbidden", "只有运营方可以变更计划")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能修改其他运营方计划")
            self._require_unsealed(conn, plan)
            if plan["status"] in {"canceled", "expired"}: raise ApiError(409, "plan_closed", "已取消或过期计划不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化")
            route = validate_route(body.get("route", self._route(plan)))
            start = parse_time(body.get("starts_at", plan["starts_at"])); end = parse_time(body.get("ends_at", plan["ends_at"]))
            if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "新飞行时间无效")
            payload = float(body.get("payload_kg", plan["payload_kg"])); altitude = float(body.get("max_altitude", plan["max_altitude"]))
            risk = body.get("population_risk", plan["population_risk"])
            if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5: raise ApiError(400, "invalid_plan", "变更后的载荷、高度或风险无效")
            revision = expected + 1
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), revision, iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": plan["status"]})
            if plan["status"] == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效")
            else: Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            return self.get_plan(plan_id, role, operator)

    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            self._require_unsealed(conn, plan)
            if plan["status"] == "canceled": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] == "expired": raise ApiError(409, "plan_expired", "已过期计划不能取消")
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}")
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in INVESTIGATION_STAFF: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso(); expired: list[sqlite3.Row] = []; sealed_skipped = 0
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE status='approved' AND ends_at<=?", (now,)))
            for row in rows:
                if self._active_seal(conn, row["id"]): sealed_skipped += 1; continue
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                expired.append(row)
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期")
        return {"expired": len(expired), "sealed_skipped": sealed_skipped}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in INVESTIGATION_STAFF: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = [self.get_plan(row["id"], role, operator) for row in rows]
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in INVESTIGATION_STAFF else []
        return {"plans": plans, "restrictions": restrictions, "server_time": iso()}

    # ------------------------------------------------------------ investigation

    def _plan_snapshot(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        snap = dict(plan); snap["route"] = self._route(plan); snap.pop("route_json", None)
        return snap

    def open_investigation(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """指挥官按计划开单：登记事故类型与发现时间，固定计划/审核/通知/限制检查四类快照。"""
        if role != "commander": raise ApiError(403, "investigation_forbidden", "只有指挥官可以开立调查封存单")
        accident_type = str(body.get("accident_type", "")).strip()
        freeze_reason = str(body.get("freeze_reason", "")).strip()
        if not accident_type: raise ApiError(400, "accident_type_required", "事故类型必填")
        found_at = parse_time(body.get("found_at"))
        if found_at > utcnow(): raise ApiError(400, "invalid_found_at", "发现时间不能晚于当前时间")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if self._active_seal(conn, plan_id):
                raise ApiError(409, "investigation_exists", "该计划已有进行中的调查封存单")
            plan_snapshot = self._plan_snapshot(conn, plan)
            review_snapshot = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
            notification_snapshot = [dict(r) for r in conn.execute("SELECT * FROM notifications WHERE plan_id=? ORDER BY id", (plan_id,))]
            restriction_snapshot = {
                "at": iso(),
                "found_at": iso(found_at),
                "report": self._conflict_report(conn, plan),
                "active_restrictions": [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id")],
            }
            cur = conn.execute("""INSERT INTO investigations(plan_id,opened_by,opened_role,accident_type,found_at,freeze_reason,
                                  plan_snapshot_json,review_snapshot_json,notification_snapshot_json,restriction_snapshot_json,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                               (plan_id, actor, role, accident_type, iso(found_at), freeze_reason,
                                json.dumps(plan_snapshot, ensure_ascii=False), json.dumps(review_snapshot, ensure_ascii=False),
                                json.dumps(notification_snapshot, ensure_ascii=False), json.dumps(restriction_snapshot, ensure_ascii=False), iso()))
            investigation_id = cur.lastrowid
            Repository.audit(conn, plan_id, actor, role, "investigation_opened",
                             {"investigation_id": investigation_id, "accident_type": accident_type, "found_at": iso(found_at)})
            Repository.notify(conn, plan_id, "investigation_sealed",
                             f"飞行计划 {plan['callsign']} 已进入事故调查封存（调查编号 {investigation_id}），封存期间仅可查看")
            return self.get_investigation(investigation_id, actor, role, "")

    def release_investigation(self, investigation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """原开单人填结论解除；封存期间若出现新禁飞区，原批准失效并退回草稿。"""
        if role != "commander": raise ApiError(403, "investigation_forbidden", "只有指挥官可以解除调查封存")
        conclusion = str(body.get("conclusion", "")).strip()
        if not conclusion: raise ApiError(400, "conclusion_required", "调查结论必填")
        with self.repo.tx() as conn:
            inv = conn.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone()
            if not inv: raise ApiError(404, "investigation_not_found", "调查封存单不存在")
            if inv["status"] != "sealed": raise ApiError(409, "investigation_closed", "调查封存单已解除")
            if inv["opened_by"] != actor:
                raise ApiError(403, "investigation_owner_only", "只有原开单人可以解除封存", {"opened_by": inv["opened_by"]})
            plan = self._plan_row(conn, inv["plan_id"])
            plan_snapshot = json.loads(inv["plan_snapshot_json"])
            frozen_ids = {r["id"] for r in json.loads(inv["restriction_snapshot_json"])["active_restrictions"]}
            bbox = route_bbox(plan_snapshot["route"]); start, end = parse_time(plan_snapshot["starts_at"]), parse_time(plan_snapshot["ends_at"])
            if frozen_ids:
                placeholders = ",".join("?" * len(frozen_ids))
                restriction_rows = conn.execute(f"SELECT * FROM restrictions WHERE status='active' AND kind='no_fly' AND id NOT IN ({placeholders})", tuple(frozen_ids))
            else:
                restriction_rows = conn.execute("SELECT * FROM restrictions WHERE status='active' AND kind='no_fly'")
            new_conflicts: list[dict[str, Any]] = []
            for restriction in restriction_rows:
                rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
                if not boxes_overlap(bbox, rbox): continue
                if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
                if not (plan_snapshot["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0): continue
                new_conflicts.append({"restriction_id": restriction["id"], "name": restriction["name"], "reason": restriction["reason"]})
            approval_voided = False
            if plan["status"] == "approved" and plan_snapshot["status"] == "approved" and new_conflicts:
                conn.execute("UPDATE flight_plans SET status='draft',updated_at=? WHERE id=?", (iso(), plan["id"]))
                approval_voided = True
            conn.execute("UPDATE investigations SET status='released',conclusion=?,released_by=?,released_at=? WHERE id=?",
                         (conclusion, actor, iso(), investigation_id))
            detail: dict[str, Any] = {"investigation_id": investigation_id, "conclusion": conclusion, "new_no_fly_conflicts": new_conflicts, "approval_voided": approval_voided}
            Repository.audit(conn, plan["id"], actor, role, "investigation_released", detail)
            Repository.notify(conn, plan["id"], "investigation_released",
                             f"飞行计划 {plan['callsign']} 的事故调查封存已解除（调查编号 {investigation_id}）")
            if approval_voided:
                Repository.notify(conn, plan["id"], "approval_invalidated",
                                 f"飞行计划 {plan['callsign']} 封存期间出现新禁飞区，原批准失效并退回草稿")
            return self.get_investigation(investigation_id, actor, role, "")

    @staticmethod
    def _snapshot_summary(inv: sqlite3.Row) -> dict[str, Any]:
        plan_snapshot = json.loads(inv["plan_snapshot_json"])
        review_snapshot = json.loads(inv["review_snapshot_json"])
        notification_snapshot = json.loads(inv["notification_snapshot_json"])
        restriction_payload = json.loads(inv["restriction_snapshot_json"])
        return {
            "plan": {
                "revision": plan_snapshot["revision"], "status": plan_snapshot["status"], "callsign": plan_snapshot["callsign"],
                "route_points": len(plan_snapshot["route"]),
            },
            "reviews": {"count": len(review_snapshot), "approved": sum(1 for r in review_snapshot if r["decision"] == "approved")},
            "notifications": {"count": len(notification_snapshot)},
            "restrictions": {
                "checked_at": restriction_payload["at"],
                "active_count": len(restriction_payload["active_restrictions"]),
                "approvable": restriction_payload["report"]["approvable"],
                "blocking_conflicts": len(restriction_payload["report"]["blocking_conflicts"]),
            },
        }

    def _investigation_payload(self, inv: sqlite3.Row) -> dict[str, Any]:
        payload = {
            "id": inv["id"], "plan_id": inv["plan_id"], "opened_by": inv["opened_by"], "opened_role": inv["opened_role"],
            "accident_type": inv["accident_type"], "found_at": inv["found_at"], "freeze_reason": inv["freeze_reason"],
            "status": inv["status"], "conclusion": inv["conclusion"], "released_by": inv["released_by"], "released_at": inv["released_at"],
            "created_at": inv["created_at"], "snapshot_summary": self._snapshot_summary(inv),
        }
        plan = self.repo.conn.execute("SELECT callsign,operator_id,status FROM flight_plans WHERE id=?", (inv["plan_id"],)).fetchone()
        if plan: payload["plan"] = {"callsign": plan["callsign"], "operator_id": plan["operator_id"], "current_status": plan["status"]}
        return payload

    def list_investigations(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT i.* FROM investigations i JOIN flight_plans p ON p.id=i.plan_id
                                             WHERE p.operator_id=? ORDER BY i.id DESC""", (operator,))
        elif role in INVESTIGATION_STAFF:
            rows = self.repo.conn.execute("SELECT * FROM investigations ORDER BY id DESC")
        else:
            raise ApiError(403, "investigations_forbidden", "当前角色不能查看调查清单")
        return {"investigations": [self._investigation_payload(r) for r in rows]}

    def get_investigation(self, investigation_id: int, actor: str, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        inv = conn.execute("SELECT * FROM investigations WHERE id=?", (investigation_id,)).fetchone()
        if not inv: raise ApiError(404, "investigation_not_found", "调查封存单不存在")
        if role == "operator":
            plan = conn.execute("SELECT operator_id FROM flight_plans WHERE id=?", (inv["plan_id"],)).fetchone()
            if not plan or plan["operator_id"] != operator: raise ApiError(403, "investigation_forbidden", "不能查看其他运营方调查")
        elif role not in INVESTIGATION_STAFF:
            raise ApiError(403, "investigation_forbidden", "当前角色不能查看调查封存单")
        return self._investigation_payload(inv)
