# 多源数据采集与RPA自动化平台

面向网页抓取、API接入、增量采集、RPA流程编排、数据校验与采集监控的数据工程平台。

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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源配置 | `datasource` | 数据源 | 源端编号、连接类型、接入地址 |
| 网页采集规则 | `crawler` | 采集规则 | 规则编号、目标网址、页面选择器 |
| API接入管理 | `apiclient` | API客户端 | 客户端编号、接口名称、请求方法 |
| 增量采集 | `incremental` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA任务编排 | `robot_task` | RPA任务 | 任务编号、任务名称、页面定位器 |
| 数据接入通道 | `ingestion` | 接入通道 | 通道编号、通道名称、输入格式 |
| 格式解析器 | `parser` | 解析配置 | 解析编号、输入格式、输出格式 |
| 采集结果校验 | `validator` | 校验规则 | 规则编号、校验字段、校验类型 |
| 采集去重 | `dedup` | 去重策略 | 策略编号、输入来源、去重键 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联任务、调度周期 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、采集源总数、今日采集量 |
| 采集日志 | `log_viewer` | 日志条目 | 日志编号、关联任务、日志级别 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、凭证类型、关联数据源 |
| 采集转换管道 | `transform` | 转换节点 | 节点编号、输入来源、输出目标 |
| 数据流转编排 | `flow` | 流转管道 | 管道编号、管道名称、起始节点 |
| 数据血缘追踪 | `lineage` | 血缘关系 | 关系编号、上游节点、下游节点 |
| 采集报告 | `report` | 采集报告 | 报告编号、采集源、统计周期 |

## 依赖任务与调度状态绑定

采集调度不再只看开始时间：每个调度计划绑定「关联任务」和「依赖任务」，依赖完成
状态由 `backend/app/services/dependency.py` 统一解析，是运行计划与等待清单的唯一依据。

### 状态与阶段

| 计划状态 | 阶段 | 含义 |
| --- | --- | --- |
| 草稿 | 草稿待依赖 | 已登记，等待确认排期 |
| 已排期 | 等待依赖 | 依赖未完成，停在已排期并生成等待清单 |
| 已排期 | 暂运行待回执 | 已到开始时间且依赖延迟，进入独立暂运行阶段并持续读取依赖回执 |
| 已排期 | 已就绪 | 依赖已完成，等待推进动作 |
| 运行中 | 运行中 | 依赖已完成且已推进，可产出运行批次 |
| 已完成 / 已暂停 | 同名 | 终态；只有这两个状态允许归档 |

关键边界：

- 依赖未完成时「启动调度」直接拒绝，计划不会进入运行中；
- 没有依赖回执依据的计划即使处于运行中，也不能产出运行批次；
- 同一计划同一分区（`plan_id:分区`）只产出一个运行批次，重复操作被拒绝；
- 归档只发生在已完成/已暂停，恢复只针对已暂停的已归档计划；
- 重算按计划的「适用范围」决定边界（`仅本计划` / `同依赖任务`），旧版本快照原样保留。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/schedule/run-plans?include_history=` | 调度运行计划快照，可选历史版本 |
| GET | `/api/schedule/waiting-list` | 依赖等待清单 |
| GET | `/api/schedule/{id}/dependency` | 依赖完成情况、计划状态与等待原因 |
| GET | `/api/schedule/{id}/compare` | 比较「停在已排期 / 进入暂运行」两个边界（只读） |
| POST | `/api/schedule/{id}/recalculate` | 按适用范围重算并推进状态 |
| POST | `/api/schedule/receipts` | 登记依赖任务结果，触发引用该任务的计划重算 |
| GET | `/api/schedule/receipts/{id}` | 依赖回执明细与同名任务日志 |
| POST | `/api/schedule/{id}/batches` | 产出运行批次（要求依赖完成依据，幂等） |
| POST | `/api/schedule/{id}/archive`、`/restore`、`/complete` | 归档、恢复、完成 |
| GET | `/api/log_viewer/dependency-waiting` | 日志侧读取依赖等待清单 |
| GET | `/api/log_viewer/verify` | 读取运行计划与等待清单并返回核对状态 |
| POST | `/api/log_viewer/sync-waiting-logs` | 为延迟依赖补关联任务日志（幂等） |
| POST | `/api/log_viewer/sync-workbench` | 生成待审核工作台项（幂等） |

调度页可以查看依赖完成情况、计划状态与等待原因，并直接打开关联任务日志或依赖回执；
日志页按等待清单补关联任务日志、生成待审核工作台项，并能打开对应依赖回执。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
