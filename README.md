# 数据仓库ETL任务编排平台

面向数据仓库ETL任务开发、作业编排调度、任务依赖解析、源端数据校验、异常任务重跑、血缘分板与运行全景监控的ETL工程管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| ETL作业 | `etljob` | 作业定义 | 作业编号、作业名称、源端数据表 |
| 任务编排 | `dag` | 编排图 | 图编号、图名称、拓扑结构 |
| 依赖解析 | `dependency` | 依赖关系 | 关系编号、上游作业、下游作业 |
| 源端校验 | `sourcecheck` | 校验配置 | 配置编号、源端数据表、行数下限 |
| 运行实例 | `runtime` | 运行记录 | 记录编号、作业名称、开始时间 |
| 异常重跑 | `rerun` | 重跑任务 | 任务编号、原始运行编号、重跑区间开始 |
| 基线管理 | `baseline` | 基线定义 | 基线编号、基线名称、承诺完成时间 |
| 分区检测 | `partitioncheck` | 检测规则 | 规则编号、数据表名、期望分区数 |
| 数据比对 | `comparator` | 比对任务 | 任务编号、源表名、目标表名 |
| 元数据存储 | `metastore` | 元数据表 | 表编号、表名、所属数仓层 |
| 资源配置 | `resource` | 资源池 | 池编号、池名称、最大并发数 |
| SLA监控 | `slavalidate` | SLA规则 | 规则编号、适用作业、完成截止时间 |
| SQL模板 | `sqltpl` | 模板定义 | 模板编号、模板名称、模板类型 |
| 数据备份 | `backuprestore` | 备份策略 | 策略编号、数据表名、备份周期 |
| 日志归档 | `logarchive` | 归档策略 | 策略编号、日志类型、保留天数 |
| 通知渠道 | `notifychan` | 渠道配置 | 渠道编号、渠道类型、接收地址 |
| 发布计划 | `publishplan` | 发布单 | 发布编号、发布内容、变更说明 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 基线预警重算（工作日 / 节假日 / 统一时区口径）

旧口径直接按自然日相减，周末、月末与节假日偏移会提前判破线并产生重复预警，跨时区
承诺时间也没有统一口径。现在改为从**基线承诺时间**与**依赖触发数据**重算，全平台只
维护一份基线预警结果数据（内存表 `baseline_alert`），基线管理与依赖解析共享同一水位。

- 规则引擎 `app/services/calendar_rules.py`
  - 工作日规则、节假日、补班日按「规则批次」管理；同一日期多批次冲突时，以**到达时间
    最晚**的生效批次为准（晚到依赖/规则修正早到口径），草稿批次不参与裁决。
  - 所有时间对内统一转 UTC 比较、对外按批次标准时区展示；跨时区承诺（如
    `2026-11-17T18:00:00-05:00`）先归一化再判定。
  - 承诺时刻落在非工作日时按基线「节假日偏移」顺延/提前到工作日，**禁止按自然日直接
    破线**；结果里的 `自然日差_仅展示_不参与判定` 只作对照。
- 重算引擎 `app/services/baseline_alert.py`
  - 三档水位：正常 / 即将破线 / 已破线；边界按节假日修正后的承诺时刻与预警余量判定。
  - 已完成运行的历史判定**冻结保留**，规则换版只重算未完成运行。
  - 同一关键下游重复扫描只通知一次（`已通知关键下游` 台账），结果按
    （基线编号, 运行编号）就地更新，不产生重复预警；晚到触发按到达顺序取最新，
    延迟修正后水位回落并在 `历史判定` 留痕。
  - 基线停用联动退役未冻结预警（记录保留、`生效标志=false`），重新启用后恢复。
- 依赖解析 `GET /api/dependency/shared-water-level` 直接读 `baseline_alert`
  标注关键下游延迟，不另算水位；基线时间线 `GET /api/baseline/timeline`
  与 `GET /api/baseline/waterline` 读同一份结果，交付时两模块水位与来源结果 id 可核对。

### 基线预警相关接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/baseline/alerts/recompute` | 从承诺时间与依赖触发重算（可带 `baseline_id` 单条重算） |
| GET | `/api/baseline/alerts` | 分页读取预警结果，支持 `water_level`、`active_only` 过滤 |
| GET | `/api/baseline/waterline` | 基线管理侧共享水位（同一份结果） |
| GET | `/api/baseline/timeline` | 基线时间线视图 |
| GET/POST | `/api/baseline/triggers` | 查看 / 幂等接入依赖触发数据 |
| GET | `/api/dependency/shared-water-level` | 依赖解析侧共享水位与关键下游延迟标注 |

后端测试：`cd backend && .venv/bin/python -m unittest tests.test_baseline_alert -v`

领域：数据工程
子方向：数仓与数据湖
