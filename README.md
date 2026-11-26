# 解构工坊 · 异构文件解析与字段抽取编排平台

面向异构文件投递、格式探测、编码归一、解析器路由、抽取模板版本、字段解析、坏行隔离、结构嗅探、嵌套展平、取值转型、单位归一、输出契约、结果投递、解析水位与重解析编排的数据采集接入平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
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

健康检查：`curl http://127.0.0.1:8004/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5177/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8004`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 文件投递 | `file_intake` | 投递批次 | 批次编号、来源渠道、原始文件名 |
| 格式探测 | `format_probe` | 探测结论 | 探测编号、关联批次、魔数特征、原推断格式、判定版本、改派回执编号 |
| 探测诊断 | `probe_diagnosis` | 探测诊断 | 诊断编号、尝试格式、诊断类型、诊断状态、回执编号、恢复动作 |
| 编码归一 | `encoding_norm` | 转码记录 | 转码编号、关联批次、原始编码 |
| 解析路由 | `parser_route` | 路由决策 | 决策编号、关联批次、推断格式、实际使用格式、判定版本、是否有效 |
| 抽取模板 | `extract_tmpl` | 模板版本 | 模板编号、模板名称、适用格式 |
| 字段解析 | `field_parse` | 解析字段 | 解析编号、关联批次、命中模板、是否正式结果、字段来源、回执编号 |
| 坏行隔离 | `row_quarantine` | 隔离行 | 隔离编号、关联批次、原始行号 |
| 解析批次 | `parse_batch` | 批次记录 | 批次记录编号、关联投递批次、解析器标识、是否正式批次、实际使用格式、实际读取行数 |
| 结构嗅探 | `schema_sniff` | 结构推断 | 嗅探编号、关联批次、样本行数 |
| 分隔规则 | `delimiter_rule` | 规则条目 | 规则编号、适用格式、分隔符 |
| 嵌套展平 | `nested_flatten` | 展平路径 | 展平编号、关联模板、原始路径 |
| 取值转型 | `value_cast` | 转型规则 | 转型编号、目标类型、来源类型 |
| 单位归一 | `unit_normalize` | 归一系数 | 归一编号、字段名称、原始单位 |
| 输出契约 | `output_contract` | 契约条目 | 契约编号、关联批次、输出列集合 |
| 结果投递 | `result_deliver` | 投递记录 | 投递记录编号、关联契约、目标位置 |
| 解析水位 | `parse_watermark` | 水位读数 | 水位编号、来源渠道、已解析至位点 |
| 重解析任务 | `reparse_task` | 重解析单 | 重解析编号、关联批次、重解析原因 |
| 重复指纹 | `dup_fingerprint` | 指纹记录 | 指纹编号、关联批次、内容指纹 |
| 抽样复核 | `sample_review` | 复核样本 | 样本编号、关联批次、抽样比例 |
| 解析指标 | `parse_metric` | 指标读数 | 指标读数编号、统计窗口、来源渠道 |
| 模板差异 | `tmpl_diff` | 差异条目 | 差异编号、旧模板版本、新模板版本 |
| 投递审计 | `intake_audit` | 审计条目 | 审计编号、关联对象、操作类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 人工格式改派工作流

值班员在「格式探测」页通过专用改派控制台处理在途作业，避免结论改变后仍从旧格式
入口提交正式结果。全部流程由 `app/routers/probe_flow.py` 承载，与通用资源动作分开：

1. `GET /api/format_probe/inflight/{批次}`：核对服务端识别到的在途解析、各格式尝试、
   有效路由、唯一正式批次与改派回执。
2. `POST /api/format_probe/{id}/confirm-reassign`：人工确认/改判格式，返回可继续字段
   解析的改派回执（含原魔数字节、原判定、判定版本、路由决策与正式批次编号）。
   重复确认幂等复用同一回执，不另排作业；已确认结论不得再改判成其他格式。
3. `POST /api/probe_diagnosis/open-compare` 与 `.../compare`：从任意格式尝试（含被替代
   的旧尝试）打开并提交差异比对；旧尝试字段未回来时进入「待恢复」并给出恢复动作。
4. `POST /api/probe_diagnosis/{id}/attempt-fields`：在途/迟到字段回到工作台，旧尝试
   字段明确标记「旧尝试」只留档，不计入正式批次成功行。
5. `GET /api/probe_diagnosis/{id}/recovery`：重新登录后回查，要么取回旧尝试字段，
   要么拿到「按改派回执重新发起诊断」的可执行恢复动作。
6. `GET /api/reassign-receipt/{回执编号}`：字段解析详情页再次进入时打开同一份回执。
7. `POST /api/field_parse/formal-submit`：XLSX 等正式解析入口凭回执提交正式字段，
   与诊断操作分别处理；旧格式入口提交会被拒绝，重复提交幂等不增加成功行。

并发一致性保证：CSV/XLSX 派发并发时解析批次台账只保留唯一正式批次，异格式在途批
一律作废（已交付目标的旧批保持原样）；解析路由写入实际使用格式与判定版本，旧路由
标记失效但保留可回查。

领域：数据工程
子方向：数据采集、爬虫与RPA
