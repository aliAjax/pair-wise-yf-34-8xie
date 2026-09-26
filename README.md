# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。事故后由指挥官开立调查封存单，固定事发时的计划、审核、通知和限制检查快照；封存期间仅可查看，任何状态改写返回 409；原开单人填结论解除时若封存期间出现新禁飞区，原批准失效并退回草稿。

## 模块划分

判定、存取和页面分开维护：

- `kernel.py`：常量、错误类型、时间/航线等纯函数。
- `store.py`：存取层，SQLite 建表、事务、审计与通知。
- `service.py`：判定层，飞行计划工作流与调查封存规则。
- `httpapp.py`：HTTP 路由与 JSON 编解码。
- `static/index.html`：协调台页面（调查清单、冻结原因、快照摘要）。
- `app.py`：启动入口并向后兼容重新导出。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知。
- `POST /api/plans/{id}/investigation/seal`：指挥官开立调查封存单（`accident_type`、`found_at`、`freeze_reason`），固定计划、审核、通知、限制检查四类快照；封存期间仅可查看。
- `POST /api/investigations/{id}/release`：原开单人填写 `conclusion` 解除；若封存期间出现与计划时空重叠的新禁飞区，原批准失效并退回草稿。
- `GET /api/investigations`、`GET /api/investigations/{id}`：调查清单（含冻结原因与快照摘要）与详情。封存期间 submit/approve/reject/change/cancel 均返回 409 `plan_sealed`。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理（到期处理自动跳过封存中的计划）。
- `GET /api/state`：按角色返回计划、限制和公开信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
