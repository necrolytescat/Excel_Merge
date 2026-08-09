# M2-05 单工作簿语义 Diff 接入契约与交接

> 状态：阶段 A/B/C 已完成，阶段 D 待独立设计  
> 更新日期：2026-08-05  
> 接入范围：`/compare/results` 单工作簿真实结果  
> 稳定输出契约：`m2.diff.v1`

## 1. 交接结论

M2-00 的 Web 页面结构已经完成，M2-01 至 M2-04 的本地单工作簿语义 Diff 也已经完成。下一步不是继续设计页面或重写 Diff，而是在两者之间增加一层只读 Web API 编排：

```text
Web 快照任务上下文
→ 单工作簿比较请求
→ 根据冻结端点与 Revision 准备 Excel + TableCsv 数据集
→ WorkbookDiffService.compare_local(...)
→ 原样返回 m2.diff.v1
→ 结果页映射并展示
```

首轮只接一个左右都存在的 `modified` 工作簿。先用固定 AtlasConfig 数据集打通“API → 正式结果页”，再接 SVN 冻结 Revision 数据源，最后实现“比对全部”的后台任务。不要同时开发 Web 映射、SVN 数据适配和批量调度。

### 1.1 阶段 A/B/C 实施结果

- 已增加严格请求模型与 `POST /api/diff/workbooks/compare`；
- 已通过可替换数据集解析依赖调用现有 `WorkbookDiffService`；
- HTTP 200 成功 body 直接使用 `serialize_diff_json()` 输出的 `m2.diff.v1`；
- 正式 `/compare/results` 已接通单工作簿请求和纯结果映射；
- `partial` 保留可用 Sheet，`failed` 与网络/编排错误不会降级为空结果；
- 固定 AtlasConfig 浏览器验收为 16 个 Sheet、273 个修改行、375 个修改字段；
- 已接入冻结 Revision 的只读 SVN 数据集物化：按每侧 `main` 精确读取对应 `TableCsv`；
- 所有 Provider 读取均使用请求 Revision，不调用 `info()`、不重新解析 `HEAD`；
- 请求级临时目录在成功或异常后清理，固定 AtlasConfig 与 Provider fixture 前后哈希不变；
- 缺 CSV、非法清单、重复主键仍返回 HTTP 200 `partial/failed`；
- 全量回归为 `178 passed`；
- 未开始“比对全部”、批量任务或 `m2.batch.v1`。

本地 API/页面验证命令：

```powershell
py -3 -m app.tools.diff_web_sample --port 5571
```

## 2. 当前真实状态

### 2.1 已完成

- `/compare` 已负责端点选择、冻结快照和全部文件级候选；
- `POST /api/diff/workbooks/compare` 已直接返回 `m2.diff.v1`；
- `/compare/results` 已消费真实结果并展示工作簿、Sheet、行/字段和详情；
- 生产应用默认从动态端点注册表解析请求中的端点和冻结 Revision；
- SVN 适配层按左右各自的 `TABLE` 绑定、同级 `TableCsv` 和 `main.tbxName` 精确物化；
- `partial` 保留可用 Sheet，临时数据集在请求结束后清理；
- `WorkbookDiffService.compare_local(source_directory, target_directory, workbook_name)` 已可生成稳定结果；
- `app/schemas/diff.py` 已冻结 `m2.diff.v1`，未知字段会被拒绝；
- 固定样例为 `tests/excel/left` 与 `tests/excel/right` 下的 `AtlasConfig.xlsm + 16 CSV`；
- 固定结果为 16 个 Sheet、56 个 source-only 行、39 个 target-only 行、273 个修改行、375 个修改字段。

### 2.2 尚未完成

- “比对全部”尚无后台任务、进度、失败隔离和结果保存契约；
- `m2.batch.v1` 尚未设计或冻结；
- 批量结果持久化和 `result_ref` 尚未实现。

## 3. 三层职责边界

| 层 | 输入 | 职责 | 禁止事项 |
|---|---|---|---|
| Web 页面 | 快照上下文、候选路径、API 结果 | 发起请求、显示状态、将 `m2.diff.v1` 映射为页面视图 | 不提交本地目录，不计算或补造语义差异 |
| Web API 编排层 | 端点 ID、冻结 Revision、工作簿逻辑相对路径 | 校验请求、准备两侧数据集、调用服务、转换 HTTP 错误 | 不重写 Diff 规则，不重新读取 HEAD |
| Diff 服务/核心层 | 两侧本地数据集目录、纯工作簿名 | 解析 `main`、读取 CSV、计算并返回 `DiffResultPayload` | 不依赖页面状态，不访问任意用户路径，不写 SVN/Excel |

`m2.diff.v1` 是唯一的工作簿明细传输契约。前端可以构建临时 view model 方便渲染，但不得保存或发布第二套 Diff JSON。

## 4. 接入前置数据

一次真实单工作簿比较必须具备：

1. `source.endpoint_id` 与 `target.endpoint_id`；
2. 两侧已经冻结的具体 Revision，不能是 `HEAD`；
3. 工作簿相对各自逻辑 `TABLE` 根目录的统一路径；
4. 两侧该工作簿的原始内容；
5. 两侧工作簿 `main` 清单映射出的全部 `{tbxName}.csv`；
6. 每侧 Excel 与 CSV 必须来自同一端点、同一冻结 Revision。

页面只传快照身份和工作簿逻辑路径。物理 `Table`/`TableCsv` 路径、本地临时目录、SVN URL 和凭证全部由服务端解析。

### 4.1 当前必须补齐的页面上下文

正式结果页的任务上下文至少保留：

```json
{
  "source": {
    "endpointId": "KR_FIX_KR-Fix-1.0.0.0",
    "resolvedRevision": 123456
  },
  "target": {
    "endpointId": "KR_FIX_KR-Fix-1.0.1.0",
    "resolvedRevision": 123789
  },
  "candidates": [
    {
      "path": "AtlasConfig.xlsm",
      "status": "modified"
    }
  ]
}
```

`sessionStorage` 只是页面恢复数据，不是安全边界。API 必须重新校验端点、Revision、路径和候选资格，不能信任浏览器提交的标签、分支名或文件元数据。

## 5. 首个 API 契约

### 5.1 路由和执行方式

```http
POST /api/diff/workbooks/compare
Content-Type: application/json
```

首版采用同步单工作簿请求，成功响应直接返回完整 `m2.diff.v1`。单工作簿固定样例约 467 KB，当前不需要拆分 Sheet API。批量比较必须另建异步任务接口，不能让浏览器并发调用此路由代替后台编排。

### 5.2 请求

建议冻结以下请求模型，所有模型使用 `extra="forbid"`：

```json
{
  "schema_version": "m2.workbook-compare.request.v1",
  "request_id": "a7e47a49-3308-4d10-936c-bbb80e4547b3",
  "source": {
    "endpoint_id": "KR_FIX_KR-Fix-1.0.0.0",
    "revision": 123456
  },
  "target": {
    "endpoint_id": "KR_FIX_KR-Fix-1.0.1.0",
    "revision": 123789
  },
  "workbook_path": "AtlasConfig.xlsm"
}
```

字段口径：

| 字段 | 必填 | 口径 |
|---|---:|---|
| `schema_version` | 是 | 固定为 `m2.workbook-compare.request.v1` |
| `request_id` | 是 | UUID，用于日志关联和重复请求排查；不写入 `m2.diff.v1` |
| `source.endpoint_id` | 是 | M1 已登记且启用的左侧端点 ID |
| `source.revision` | 是 | 正整数冻结 Revision，不接受 `HEAD` |
| `target.endpoint_id` | 是 | M1 已登记且启用的右侧端点 ID |
| `target.revision` | 是 | 正整数冻结 Revision，不接受 `HEAD` |
| `workbook_path` | 是 | 相对逻辑 `TABLE` 根目录的路径，使用 `/`；不能是绝对路径或包含 `..` |

`workbook_path` 可以包含工作簿子目录；传给当前 `compare_local()` 时，编排层在每次请求独立的临时数据集中使用纯文件名。不得把浏览器传入的路径直接与本地根目录拼接。

### 5.3 成功响应

HTTP 200 的 body 直接为 `DiffResultPayload`，不增加 `data/result/payload` 包装：

```json
{
  "schema_version": "m2.diff.v1",
  "direction": {"source": "left", "target": "right"},
  "workbook": {
    "name": "AtlasConfig.xlsm",
    "status": "modified",
    "source_sha256": "...",
    "target_sha256": "..."
  },
  "summary": {
    "total_sheets": 16,
    "unchanged_sheets": 7,
    "modified_sheets": 9,
    "source_only_sheets": 0,
    "target_only_sheets": 0,
    "failed_sheets": 0,
    "source_only_rows": 56,
    "target_only_rows": 39,
    "modified_rows": 273,
    "modified_fields": 375,
    "error_count": 0
  },
  "sheets": [],
  "errors": []
}
```

完整字段示例以 `docs/contracts/m2.diff.v1.example.json` 和 `app/schemas/diff.py` 为准。API 必须使用 `serialize_diff_json()` 的 UTF-8 输出，不能在路由层改名、删除字段或追加时间戳。

### 5.4 HTTP 状态与业务状态分层

| HTTP | 含义 | 响应 |
|---:|---|---|
| 200 | 引擎已经执行 | `m2.diff.v1`；`workbook.status` 可为 `unchanged/modified/partial/failed` |
| 400 | JSON、版本或路径语法非法 | Web 编排错误结构 |
| 404 | 端点、Revision 下工作簿或快照上下文不存在 | Web 编排错误结构 |
| 409 | 页面携带的 Revision 与任务上下文不一致或上下文已过期 | Web 编排错误结构 |
| 422 | 候选不允许做单工作簿语义比较，例如单侧文件或读取失败 | Web 编排错误结构 |
| 500 | 未被稳定 M2 错误契约覆盖的编排异常 | Web 编排错误结构，服务端记录 `request_id` |

Web 编排错误沿用简单结构：

```json
{
  "error": {
    "code": "DIFF_INVALID_WORKBOOK_PATH",
    "message": "工作簿路径不合法"
  }
}
```

CSV 缺失、CSV 解析失败、清单解析失败等引擎可表达的业务失败不转成 4xx/5xx。它们仍返回 HTTP 200，并通过 `workbook.status`、`sheets[].status` 和 `errors[]` 表达。这样 `partial` 中已成功的 Sheet 不会丢失。

## 6. 候选范围与当前契约缺口

M1 候选状态与首版处理方式如下：

| M1 候选状态 | 首版单工作簿 API | 原因 |
|---|---|---|
| `modified` | 支持 | 左右工作簿都存在，符合当前服务输入 |
| `left_only` | 暂不调用语义引擎 | `m2.diff.v1` 的 workbook 状态没有 `source_only` |
| `right_only` | 暂不调用语义引擎 | `m2.diff.v1` 的 workbook 状态没有 `target_only` |
| `read_error` | 暂不调用语义引擎 | 工作簿内容不完整，不能伪造成空差异 |

结果页仍应保留后三类工作簿，并显示明确的文件级状态；首版可映射为 `diff_unavailable` 并说明“不适用/无法执行”，不能将其显示为 `diff_empty`。

在实现“比对全部”前，必须在批量契约中决定如何表示单侧工作簿和 M1 读取失败。建议由后续 `m2.batch.v1` 保存文件级状态和可选的 `result_ref`，不要破坏已经冻结的 `m2.diff.v1`。

## 7. 服务端数据集准备口径

SVN 适配层按每一侧独立执行：

1. 通过 `endpoint_id` 从端点注册表取 URL 和 `TABLE` 物理绑定；
2. 使用请求中的具体 Revision 创建只读 `EndpointSpec`，禁止重新解析 `HEAD`；
3. 规范化 `workbook_path` 并确认它位于逻辑 `TABLE` 范围内；
4. 读取该工作簿原始字节并解析 `main`；
5. 按 `dataset_layout.csv_export.directory_name` 定位同端点的 `TableCsv`；
6. 只读取 `main` 中导出项对应的 `{tbxName}.csv`，不扫描全目录猜测归属；
7. 将工作簿和 CSV 放入本次请求的隔离临时目录；
8. 调用现有 `WorkbookDiffService.compare_local()`；
9. 响应完成后清理临时目录，不修改原始文件。

生产页面不得增加“本地样本入口”。固定 AtlasConfig 样本只允许用于自动化测试、依赖注入或本地 API 验证。

### 7.1 候选校验

当前 M1 没有服务端持久化的 `snapshot_id`。首版 API 应使用“端点 ID + 冻结 Revision + 逻辑路径”定位不可变内容，并在服务端按 M1 规则重新确认两侧文件均存在且属于可比较 Excel。若后续增加服务端快照索引，则改为校验候选属于该 `snapshot_id`，但不能把 `sessionStorage` 当作凭证。

## 8. `m2.diff.v1` 到结果页的映射

### 8.1 页面状态

| 条件 | 页面状态 | 展示要求 |
|---|---|---|
| 尚未请求 | `diff_unavailable` | 明确“未执行” |
| 请求中 | `diff_loading` | 禁用当前工作簿按钮，保留任务上下文 |
| `workbook.status=unchanged` | `diff_empty` | 明确“已执行且无语义差异” |
| `workbook.status=modified` | `diff_ready` | 展示 Sheet、行、字段和详情 |
| `workbook.status=partial` | `diff_error`，但保留数据 | 展示可用 Sheet，同时展示失败 Sheet 和 `errors` |
| `workbook.status=failed` | `diff_error` | 展示结构化错误，不能降级成空结果 |
| HTTP/网络失败 | `diff_error` | 展示 Web 编排错误，允许重试 |

当前结果页的 `diff_error` 会清空 Sheet 导航。接入时必须调整：`partial` 不能清空已成功的 `sheets[]`。

### 8.2 字段映射

| 页面内容 | `m2.diff.v1` 来源 | 规则 |
|---|---|---|
| 工作簿名 | `workbook.name` | 不再从路径重复推导 |
| 工作簿摘要 | `summary.*` | 使用服务端统计，不在前端重复聚合 |
| Sheet 名 | `sheets[].sheet_name` | 同时作为导航稳定键 |
| Sheet 状态 | `sheets[].status` | 支持 unchanged、modified、source_only、target_only、failed |
| Sheet 差异数 | `sheets[].summary` | 分别显示行与字段，不只计算 `changes.length` |
| 主键字段 | `sheets[].primary_key` | 仅用于说明当前行键口径 |
| 行键 | `sheets[].rows[].key` | 直接显示，不生成业务名称 |
| 行状态 | `sheets[].rows[].status` | modified、source_only、target_only |
| 修改字段 | `rows[].changes[].field` | 仅 modified 行有字段修改列表 |
| 左右值 | `changes[].source/target` | 页面文案使用左侧/右侧或实际分支名 |
| 单侧整行值 | `rows[].source.values` / `target.values` | 展示真实字段值，不伪造“整行”字段 |
| 详情定位 | `sheet_name + field + source/target.row_number` | 当前契约没有 Excel 列字母，不能伪造 `A1` 地址 |
| 字段定义 | `sheets[].fields[]` | 展示类型、范围和字段单侧状态 |
| 错误 | 工作簿及 Sheet 的 `errors[]` | 保留 code、stage、side、file 和 message |

方向词固定为 `source=left`、`target=right`。页面可以把左右值标题替换为真实分支名，但不得使用文件时间推断 OLD/NEW。

## 9. 推荐实施顺序

### 阶段 A：固定样本打通单工作簿 API

1. 在 `app/schemas` 增加上述请求模型和契约测试；
2. 在 `app/api` 增加同步单工作簿路由；
3. 用可替换的数据集解析依赖，在测试中把合法请求映射到固定 AtlasConfig 左右目录；
4. 路由调用现有 `WorkbookDiffService` 并直接返回 `m2.diff.v1`；
5. 覆盖 200 modified、unchanged、partial、failed 以及 4xx 编排错误；
6. 不增加生产页面的本地样本选择入口。

### 阶段 B：正式结果页消费真实结果

1. 在任务上下文中保留两侧 `endpoint_id` 和冻结 Revision；
2. 为 `compare_results.js` 增加纯映射函数，直接消费 `m2.diff.v1`；
3. 接通“比对当前工作簿”；
4. 覆盖 loading、empty、ready、partial、failed 和网络失败；
5. 正式模式删除所有对 Demo `results/sheets/rows/fields` 结构的依赖；Demo 可继续独立存在，但不能进入正式结果页。

### 阶段 C：冻结 Revision 的 SVN 数据适配

已完成：

1. 增加内部数据集解析/物化服务，未修改 `core` 或冻结 Diff 规则；
2. 从同一端点同一请求 Revision 读取工作簿和映射 CSV；
3. 使用请求级临时隔离目录适配当前 `compare_local()`，成功和异常均清理；
4. 已验证缺 Excel、缺 CSV、非法清单、重复主键和 SVN 读取失败；
5. 已验证执行前后 Provider fixture、固定 Excel 和 CSV 均未改变。

### 阶段 D：比对全部

1. 冻结 `m2.batch.v1`，明确任务状态、进度、单侧文件和失败隔离；
2. 后端逐工作簿调度，每个可比较工作簿保存独立 `m2.diff.v1`；
3. 批量结果只返回摘要与 `result_ref`，不拼接所有明细；
4. 主页面“比对差异”创建批量任务并进入结果页；
5. 结果页“比对当前工作簿”复用单工作簿 API，负责测试和重试。

## 10. 验收清单

- [x] 请求不接受本地绝对路径、`..`、URL、`HEAD` 或未知字段；
- [x] source/target 始终对应页面左/右端点，不由时间推断；
- [x] Web API 不重新实现 Sheet、行或字段匹配规则；
- [x] 成功 body 可被 `DiffResultPayload.model_validate()` 完整校验；
- [x] 200 `partial/failed` 不被错误转换成 HTTP 500；
- [x] `partial` 的成功 Sheet 仍可查看；
- [x] `source_only/target_only` 行不伪造字段变化或单元格地址；
- [x] `left_only/right_only/read_error` 候选不冒充 `diff_empty`；
- [x] 固定 AtlasConfig 结果统计和 SHA-256 回归保持不变；
- [x] 正式结果页不读取 Demo 假数据；
- [x] 无生产页面本地样本入口；
- [x] SVN 工作簿与 CSV 始终使用请求中的具体 Revision，未调用 `info()` 或 `HEAD`；
- [x] 仅读取两侧各自 `main` 映射的 CSV，不扫描 `TableCsv` 猜测归属；
- [x] 请求级临时目录在成功和异常路径均已清理；
- [x] 不修改 `core/`、不写 Excel、不执行任何 SVN 写操作；
- [x] 全量测试通过，并增加 API 契约、结果映射和端到端状态测试。

## 11. 后续边界

阶段 A/B/C 已验收。本阶段在此停止；进入阶段 D 前必须先冻结独立的
`m2.batch.v1` 契约，明确任务状态、进度、单侧文件、失败隔离、结果保存和
`result_ref`，不能用浏览器并发调用单工作簿接口代替后台任务。
