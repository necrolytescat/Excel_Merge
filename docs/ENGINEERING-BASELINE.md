# 工程基线

> 状态：版本对比模块已交付，允许按明确需求持续维护
> 更新日期：2026-08-09

## 1. 系统基线

版本对比是本地 FastAPI + Jinja2 + 原生 JavaScript/CSS 应用。SVN Provider、快照服务、数据集物化、语义 Diff、批量编排和结果展示保持分层，前端只通过稳定 API/契约消费结果。

```text
Web/API
  -> SnapshotService
  -> BatchDiffService / WorkbookDiffService
  -> WorkbookDatasetResolver
  -> read-only SVNProvider

WorkbookDiffService
  -> workbook main manifest
  -> TableCsv parser
  -> semantic diff
  -> m2.diff.v1
```

详细文件地图见 `VERSION-COMPARISON-HANDBOOK.md`。

## 2. 数据与方向

- 左侧固定为 source，右侧固定为 target；
- source-only 表示右侧删除，target-only 表示右侧新增；
- 两侧端点在确认时分别冻结 HEAD，后续请求只使用正整数 Revision；
- Table Excel 和 TableCsv 必须来自同一端点、同一冻结 Revision；
- M1 文件候选只扫描 Table 下 `.xlsx/.xlsm/.xls`；
- 语义 Diff 只读取候选工作簿 `main` 映射到的 CSV，不扫描 TableCsv 猜测归属。

## 3. 语义基线

- Excel `main.sheetName` 是逻辑 Sheet 身份，`tbxName` 定位 CSV；
- CSV 第 1/2/3/4 条逻辑记录分别是显示名、字段名、类型、scope，第 8 条起是数据；
- `scope=none` 不进入比较；
- 主键优先唯一匹配 `Id/id`，缺失时仅允许物理第一列有效业务字段；
- 行按主键、字段按稳定字段名精确匹配；不使用行号、列位置、内容哈希或模糊匹配；
- 类型规范化只影响比较，输出保留原始字符串和逻辑行号；
- 缺 CSV、重复字段/主键、非法结构和解析失败必须成为结构化错误，不能视为无差异；
- 公式、格式、宏、隐藏状态和 Excel 业务单元格不参与业务值 Diff。

## 4. 契约基线

- 单工作簿明细：`m2.diff.v1`；
- 批量任务：`m2.batch.v1`；
- 单工作簿请求：`m2.workbook-compare.request.v1`；
- Replay 归档：`m2.fixture.v1`。

`m2.diff.v1` 是前后端和 Replay 共用的唯一 Diff JSON。破坏性字段变更必须升版；展示需求优先在 mapper/view model 层解决，不得静默修改语义汇总。

`m2.batch.v1` 只保存任务、候选事实、状态、摘要和不透明 `result_ref`。每个可执行工作簿独立生成一份原始 `m2.diff.v1`。

## 5. 只读与安全

- 禁止 `svn commit/ci/merge/update/copy` 等写命令；
- URL、JSON、日志和夹具不保存凭据；
- workbook path 必须是规范 POSIX 相对路径，拒绝绝对路径、URL、反斜杠和 `..`；
- 临时数据集在成功和异常路径都清理；
- Replay 仅开发模式注册，正式模式返回 404；
- 夹具加载必须验证 ZIP 路径、大小、压缩方式、成员集合、哈希、契约和身份；
- 不执行宏、公式或夹具中的可执行内容。

## 6. 持久化与运行

批量任务默认状态目录为 `var/m2-batch`：SQLite 保存任务与租约，结果以 gzip JSON 保存。运行数据被 Git 忽略。服务启动执行恢复和清理；非终态任务不可按年龄删除。

当前是本地单机实现：全局工作簿执行并发 2、单任务并发 1、单项超时 600 秒、进程中断最多自动恢复一次。分布式队列不在当前基线内。

## 7. 前端基线

- `/compare/results`、Demo 结果和 Replay 共用 `compare_results.html` 与结果渲染器；
- `m2_diff_mapper.js` 是唯一契约 mapper；
- 正式页从批量 API 轮询状态，最多 4 个 worker 并发读取已完成工作簿摘要，选中时按需加载明细；
- 工作簿确认态是 `sessionStorage` 审阅状态，不改变任务或 Diff；
- 行视图只渲染当前 Sheet 的可视窗口，左右滚动同步；
- Demo 数据只能用于开发预览，正式页不能引用。

已验收页面结构详见工作手册第 4.3 节。无明确需求不得顺带调整 Batch Task、工作簿导航、结果标题、Sheet 导航和行字段主区域。

## 8. 回归基线

当前冻结 Replay：

```text
var/m2-fixtures/d3c-be317423.m2fixture
Task be317423-3863-4cfe-aa6a-fc38ad50919f
source r26476 / target r26476
55 succeeded / 0 failed
728 inputs / 0 missing
55 current / 55 matched / 0 mismatched
SHA-256 092847df4c3b97f1026fe717d789a9f676e3352f1e27b904805df06682dfb0fc
```

固定本地样例位于 `tests/excel/left` 与 `tests/excel/right`。全量自动化入口为：

```powershell
py -3 -m pytest -q
```

前端修改还必须执行对应 `node --check`、`git diff --check` 和 Replay 浏览器验收。正式任务、SVN 访问、夹具导出或黄金更新需要用户明确授权。

## 9. 历史

收尾前工程基线保存在 `archive/m2-history/ENGINEERING-BASELINE-AT-M2-CLOSEOUT.md`。历史文档中的测试数、夹具路径和待办可能已过期，不能覆盖本文件和当前契约。
