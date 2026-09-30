# 地质勘探数据管理平台

面向地质勘探的钻孔编录、岩心取样、物探数据、化探分析、测绘资料与储量估算的综合数据管理后台。

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
| 钻孔编录 | `borehole` | 钻孔 | 钻孔编号、勘探区、孔口坐标 |
| 岩心管理 | `core` | 岩心样本 | 岩心编号、所属钻孔、取样深度起 |
| 地层划分 | `stratigraphy` | 地层单元 | 单元编号、钻孔编号、地层名称 |
| 地球物理 | `geophysics` | 物探测线 | 测线编号、勘探区、物探方法 |
| 化探分析 | `geochem` | 化探样品 | 样品编号、样品类型、采样点位 |
| 化验数据 | `assay` | 化验结果 | 化验编号、样品编号、元素名称 |
| 地质填图 | `mapping` | 填图单元 | 图幅编号、图幅名称、比例尺 |
| 测绘控制 | `survey_point` | 控制点 | 点号、点类型、坐标X |
| 钻探日志 | `drilling_log` | 钻探记录 | 日志编号、钻孔编号、钻进深度 |
| 储量估算 | `reserve` | 矿体块段 | 块段编号、矿体名称、面积 |
| 样品登记 | `sample_registry` | 送检样品 | 送检编号、样品名称、采样位置 |
| 勘探设备 | `equipment` | 勘探仪器 | 仪器编号、仪器名称、型号规格 |
| 水文地质 | `hydro` | 水文观测点 | 观测编号、观测类型、所在钻孔 |
| 剖面编录 | `section` | 实测剖面 | 剖面编号、剖面名称、剖面长度 |
| 地质报告 | `geological_report` | 勘探报告 | 报告编号、勘探区、报告类型 |
| 遥感解译 | `remote` | 遥感数据 | 数据编号、数据源、分辨率 |
| 矿产评价 | `mineral` | 矿化线索 | 线索编号、勘探区、矿种 |
| 环境地质 | `environmental` | 环境调查点 | 调查编号、调查区域、灾害类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 钻探日志整批导入

入口为「钻探日志」页的「整批导入」页签，后端接口统一挂在 `/api/drilling_import`：

| 接口 | 作用 |
| --- | --- |
| `POST /sessions`、`POST /sessions/{id}/rows`、`GET /sessions/{id}` | 断点续传：按行偏移量上传，断线后从服务器确认的 `received` 行继续，重复尾部覆盖、缺口拒绝 |
| `POST /preview`、`POST /sessions/{id}/preview` | 整批预览：事务内临时落库试算结论/待办后回滚，不写任何业务数据 |
| `POST /commit`、`POST /sessions/{id}/commit` | 整批提交：解析、校验、落库、结论回写与待办生成同一事务，任意一行不通过返回 422 整批退回 |
| `GET /batches` | 批次留痕：成功记录与退回记录都在，按文件 SHA-256 指纹幂等 |
| `GET /quarantine`、`POST /quarantine/{id}/resolve` | 缺钻孔编号的行先隔离，补现场编号后整批迁入台账 |
| `GET /deviations`、`POST /deviations/{id}/status` | 偏离待办清单（孔深偏离设计、回次深度异常、未登记钻孔、缺孔号待查） |

导入口径：

- **整批事务**：`store.transaction()` 采用快照栈，支持嵌套（预览事务套提交事务），
  块内异常逐表原地回滚，绝不留下半批日志或半批待办。
- **指纹幂等**：文件全文 SHA-256 为指纹，重复提交同一文件返回首次结果，不重复落库。
- **三处同一结论**：库内结论只在 `app/services/drilling_conclusion.py` 计算一次，
  同批回写到日志台账、钻孔详情、偏离待办；前端三处展示取自该结论。
- **冲突优先级**：来件与既有日志冲突时以现场终孔记录为准；历史班次按原上报基准
  保留，导入不改写既有日志行。已确认终孔深度受保护，来件终孔深度与其不一致时
  整批退回（422），不允许覆盖。
- **缺孔号隔离**：缺钻孔编号的行进隔离表并生成「缺孔号待查」，其余行按现场编号
  正常迁移。

