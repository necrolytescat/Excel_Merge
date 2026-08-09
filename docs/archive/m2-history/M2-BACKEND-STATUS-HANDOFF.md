# M2 后端状态与恢复交接

> 状态：M2-05 阶段 A/B/C 已完成，阶段 D 未开始  
> 更新日期：2026-08-05  
> 当前用途：M2 单工作簿链路完成状态与后续批量阶段边界  

## 1. 当前结论

M2 的单工作簿本地核心已经完成并验收：

```text
Excel main 清单
→ sheetName/tbxName 映射
→ 左右 CSV 严格解析
→ Id/id 精确行匹配
→ 字段名精确比较
→ m2.diff.v1 稳定 JSON
```

当前已经打通“冻结端点与 Revision → SVN 工作簿及映射 `TableCsv` → `WorkbookDiffService` → `m2.diff.v1` → 正式结果页”的单工作簿链路。
阶段 A/B/C 已按 `M2-05-WEB-DIFF-INTEGRATION-CONTRACT.md` 验收。尚未设计或实现批量任务 API。

## 2. 已完成阶段

| 阶段 | 状态 | 结果 |
|---|---|---|
| M2-01 样例验证 | 已完成 | 左右 AtlasConfig Excel+16 CSV 规则确认 |
| M2-02 JSON 契约 | 已完成 | `m2.diff.v1`、状态、错误码、规范序列化 |
| M2-03 解析层 | 已完成 | `openpyxl` 优先、最小 OOXML 兜底、严格 CSV 解析 |
| M2-04 Diff 引擎 | 已完成 | `sheetName → Id/id → 字段名`，无模糊和行号兜底 |
| M2-05 阶段 A/B | 已完成 | 单工作簿 API 与正式结果页直接消费 `m2.diff.v1` |
| M2-05 阶段 C | 已完成 | 冻结 Revision 的只读 SVN 数据集物化与请求级清理 |

## 3. 已交付实现

| 文件 | 职责 |
|---|---|
| `app/schemas/diff.py` | `m2.diff.v1` Pydantic 契约和稳定序列化 |
| `core/workbook_manifest_parser.py` | Excel `main` 清单解析 |
| `core/table_csv_parser.py` | TableCsv 严格解析 |
| `core/semantic_diff.py` | 单 Sheet 严格语义 Diff |
| `app/services/workbook_diff_service.py` | 本地单工作簿编排 |
| `app/services/workbook_dataset_service.py` | 固定绑定与冻结 Revision SVN 数据集解析/物化 |
| `app/api/diff.py` | 同步单工作簿 Web API 与临时数据集生命周期 |
| `app/tools/diff_sample.py` | 本地 JSON 输出命令 |
| `tests/integration/test_atlas_config_diff.py` | 固定真值和 SHA-256 回归 |
| `tests/unit/test_svn_workbook_dataset_resolver.py` | Revision、精确 CSV、错误分层、清理和只读回归 |

本地运行命令：

```powershell
py -3 -m app.tools.diff_sample `
  --source tests/excel/left `
  --target tests/excel/right `
  --workbook AtlasConfig.xlsm `
  --output .cache/m2/AtlasConfig.diff.json
```

## 4. 固定验收结果

| 指标 | 结果 |
|---|---:|
| JSON 大小 | 466,891 bytes |
| gzip 大小 | 32,865 bytes |
| 逻辑 Sheet | 16 |
| unchanged Sheet | 7 |
| modified Sheet | 9 |
| source-only 行 | 56 |
| target-only 行 | 39 |
| 修改行 | 273 |
| 修改字段 | 375 |
| 错误 | 0 |
| 全量测试 | 178 passed |

JSON SHA-256：

```text
430e5ae560fa6c83d6580e40f9e635294149cbdd9cacfb7020df9a2acb59a1e7
```

性能基线：

- Node `JSON.parse` 平均约 0.6 ms；
- Pydantic 完整校验平均约 2.85 ms；
- 当前有 368 条差异行、3,992 个展示值；
- 当前单工作簿读取和解析没有性能瓶颈；
- Web 应只渲染当前 Sheet，大结果使用分页或虚拟列表。

## 5. 已冻结规则

- Excel 业务 Sheet 单元格、公式、格式、宏和隐藏状态不参与 Diff；
- `main` 只用于读取 `sheetName/tbxName`；
- `配置公式2` 不参与 Diff；
- CSV 第 2/3/4 条逻辑记录为字段、类型、范围，第 8 条起为业务数据；
- CSV 引号内换行不增加逻辑记录号；
- 第 2 行末尾无代码名的注释列不参与业务 Diff；
- 主键只接受 `Id`、`id`；
- 行、字段和 Sheet 不使用位置或模糊相似度兜底；
- 输出方向固定为 `source=left`、`target=right`；
- 破坏 `m2.diff.v1` 的字段变更必须升级 schema version；
- 原始 Excel 与 CSV 永远只读。

## 6. 当前状态

### 已完成：M2-05 阶段 A/B/C

- 已增加单工作簿请求模型和 `POST /api/diff/workbooks/compare`；
- 固定 AtlasConfig 数据集已调用现有 `WorkbookDiffService`；
- API 成功响应直接返回 `m2.diff.v1`；
- 正式 `/compare/results` 已消费真实结果；
- 已覆盖 loading、empty、ready、partial、failed 和 Web 编排错误；
- 生产默认 resolver 已从动态端点注册表读取左右端点；
- 所有 SVN 读取固定使用请求 Revision，从不调用 `info()` 或重新读取 `HEAD`；
- 每侧按自己的 `main.tbxName` 精确读取同级 `TableCsv`；
- 临时数据集在成功和异常路径均清理，原始 SVN、Excel 和 CSV 保持只读；
- 缺 CSV、非法清单、重复主键仍由 `m2.diff.v1` 以 HTTP 200 表达；
- 已按边界停止，未实现“比对全部”。

### 当前下一步：阶段 D

- 为全量候选增加任务编排、进度、失败隔离和取消策略；
- 每个工作簿独立保存一份 `m2.diff.v1`；
- 设计批量汇总契约，暂定版本名 `m2.batch.v1`；
- 为报告层提供汇总和单工作簿结果引用。

`m2.batch.v1` 目前只是建议名称，尚未冻结、尚未实现。不得为了页面展示反向修改 `m2.diff.v1` 核心语义。

## 7. 全量比对的后端边界

正确模型：

```text
M1 文件级候选
→ 批量任务逐个调度
→ 每个工作簿生成独立 m2.diff.v1
→ 批量结果只保存摘要和 result_ref
→ Web 打开工作簿时再读取详细结果
```

禁止模型：

- 扫描 `TableCsv` 全目录后自行猜归属；
- 把所有工作簿明细拼成一个巨大 JSON；
- Web 一次性渲染所有工作簿的全部行；
- 批量层重新实现一套不同的 Diff 规则；
- 批量任务重新读取 HEAD。

## 8. 文件所有权状态

原 Web 与后端并行开发已经结束，旧的“Web 线程独占/后端线程独占”边界不再作为 M2-05 的执行限制。

M2-05 新对话统一负责阶段 A/B 所需的 `app/templates/**`、`app/static/**`、`app/main.py`、`app/api/**`、请求 schema 和相关测试。执行期间不要再由其他对话并行修改这些文件。

以下后端核心仍视为冻结实现，M2-05 不得修改其 Diff 规则：

- `core/workbook_manifest_parser.py`；
- `core/table_csv_parser.py`；
- `core/semantic_diff.py`；
- `core/m2_errors.py`；
- `app/services/workbook_diff_service.py`；
- `app/schemas/diff.py` 中已经冻结的 `m2.diff.v1` 响应模型。

## 9. Web 阶段性交接结果

Web 阶段性交接已经由 `M2-05-WEB-DIFF-INTEGRATION-CONTRACT.md` 完成，并冻结以下结论：

1. 首轮只比较左右都存在的 `modified` 单工作簿；
2. 请求携带两侧 `endpoint_id`、冻结 Revision 和工作簿逻辑路径；
3. 成功响应直接使用 `m2.diff.v1`，不增加第二套 Diff JSON；
4. `partial/failed` 是合法 HTTP 200 业务结果；
5. 正式结果页不使用 Demo 假数据；
6. SVN `TableCsv` 数据适配已完成，批量任务仍后置。

## 10. 下一对话执行步骤

1. 读取最新路线图、M2-05 契约和当前工作区；
2. 运行 `py -3 -m pytest -q`，以 `178 passed` 为阶段 C 交接基线；
3. 先设计并冻结 `m2.batch.v1`，不得直接开始批量实现；
4. 明确任务状态、进度、取消、单侧文件、失败隔离、保存和 `result_ref`；
5. 后续批量调度复用现有单工作簿 API/服务语义，不重新读取 `HEAD`。

阶段 C 已验收，本交接不包含任何阶段 D 实现。
