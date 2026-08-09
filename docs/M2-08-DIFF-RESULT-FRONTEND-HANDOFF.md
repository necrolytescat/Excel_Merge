# M2-08 差异结果前端改造交接

> 状态：已验收并归档
> 归档日期：2026-08-09
> 阶段归属：M2，不进入 M3
> 归档范围：Batch Task、工作簿导航、标题与 Sheet 导航、行与字段差异
> 归档记录：`docs/archive/M2-08-ROW-FIELD-DIFF-ARCHIVE.md`
> 说明：第 1 至 13 节保留阶段过程与历史启动上下文；后续状态以第 14 节为准。

## 1. 阶段定位

M2-08 基于已经稳定的真实 Diff 数据，逐步改造差异结果页的信息架构、可读性和
重复审阅效率。它不重新设计解析器、Diff 语义、批量任务或 SVN 流程。

下一对话读取本交接后，只能先恢复上下文并等待用户指定具体事项。不得因为文档中
列出了推荐方向，就自动设计或实施全部前端改造。每一项按以下节奏执行：

1. 用户指定要处理的页面问题或区域；
2. 读取当前实现和代表数据；
3. 说明现状、拟议方案、影响范围和验收方法；
4. 等待用户明确要求修改；
5. 只实施该项，不顺带扩展其他区域。

长期报告、搜索中心、导出、通知、Merge 和 SVN 写回仍属于后续阶段，不进入本轮。

## 2. 当前 M2 状态

已经完成：

- M1 冻结 Revision 和文件级候选；
- `m2.diff.v1` 单工作簿语义 Diff；
- `m2.batch.v1` 单机批量任务、持久化、取消、重试和失败隔离；
- 正式 `/compare/results` 对批量任务和按需结果的消费；
- source/target 同侧冻结 Revision 的 Excel 与 CSV 配对；
- `isExport=1`、`scope=None`、公式缓存值和方向语义；
- TableCsv 文件名大小写唯一匹配；
- 缺少唯一 `Id/id` 时使用物理第一列业务字段的受限兜底；
- Replay 离线重算和黄金结果校验；
- 全量自动化 `212 passed`。

用户已通过正式 Web 自行完成一次真实任务，并确认结果符合预期；但本对话没有记录
Task ID，因此功能验证已完成，审计记录尚未补齐。

## 3. M2 剩余内容

### 3.1 当前主任务

- Batch Task、工作簿导航、比对结果标题区和 Sheet 标题与导航已经逐项完成并锁定；
- 下一对话只按用户逐项要求改造“行与字段差异”模块；
- 使用真实 Replay 和测试数据验证每项交互；
- 保持正式、Replay、Demo 三种结果模式的共享渲染契约；
- 为确认后的改造补充自动化和浏览器验收。

### 3.2 非阻塞收尾项

- 当前离线夹具未保存 `MainActivity_FunctIonName.csv` 两侧字节，黄金任务保留
  `1 business_failed`；这是旧夹具输入缺口，不是当前代码或源数据缺陷；
- 后续应基于可审计正式任务导出输入完整的最终夹具；
- M2 结束前应记录最终 Task ID、冻结 Revision、候选数量和编排结果；
- 前端改造和最终夹具完成后，再更新路线图为 M2 完成。

这些事项不阻塞前端逐项改造。当前夹具中的已知失败也可用于验证 partial/error UI。

## 4. 测试数据入口

### 4.1 D3-C Replay 夹具

```text
var/m2-fixtures/d3c-6e501824.m2fixture
Task: 6e501824-ac7d-49d4-bd7f-6d7136a958f1
source revision: 26438
target revision: 26438
SHA-256: bde0ff57c39cf53c9370ab76b5c496f9d2129b06b9336695204b8662c501e296
```

当前黄金任务：

```text
55 results
54 succeeded
1 business_failed
Replay recompute: 55 current / 55 matched / 0 mismatched
```

旧基线审计副本：

```text
var/m2-fixtures/d3c-6e501824.pre-first-column-key.m2fixture
SHA-256: f62564f37f9101c116cf910224f1234bc2869b5df9d269d707e7684e8f509fc0
```

Replay 只在开发模式开放：`http://127.0.0.1:5566/compare/replay`。

### 4.2 代表场景

| 场景 | 样本 | 验证重点 |
|---|---|---|
| 真实字段修改 | `ShopConfig/RefreshShop` | `ShopId=510000` 的 `Key_RefreshDesc` 左右值不同，并含长文本 |
| 第一列主键、Sheet 无差异 | `HeroConfig/Reborn_TransferLevel` | 主键 `Old_level`，Sheet 为 unchanged |
| 第一列主键、工作簿无差异 | `PetConfig/ValueQuality` | 主键 `EntryQuality`，空差异状态明确 |
| partial/error | `MainActivity/FunctionName` | 两侧 `M2_CSV_MISSING` 可定位，并标注已知夹具缺口 |
| 大结果 | AtlasConfig 固定左右样本 | 16 Sheet、273 修改行、375 修改字段 |
| 单侧业务行 | AtlasConfig 固定结果 | source_only/target_only 展示真实整行字段值 |
| 批量状态 | `m2.batch.v1` 契约测试 | 运行、成功、业务失败、编排失败、跳过、取消 |

## 5. 当前结果页实现

### 5.1 路由

- `/compare`：端点、快照和候选输入；M2-08 默认不修改；
- `/compare/results`：正式批量结果页；
- `/compare/demo/results`：开发模式示例；
- `/compare/replay`：开发模式离线黄金/当前重算结果。

### 5.2 文件职责

| 文件 | 当前职责 |
|---|---|
| `app/templates/compare_results.html` | 正式、Demo、Replay 共用结构 |
| `app/static/m2_diff_mapper.js` | `m2.diff.v1` 到前端 view model 的唯一映射 |
| `app/static/compare_results.js` | 工作簿、Sheet、行、字段详情和单项重算 |
| `app/static/compare_results_batch.js` | 批量轮询、按需结果、取消和重试 |
| `app/static/offline_replay.js` | 夹具加载、结果模式切换和重算 |
| `app/static/compare_results_readability.css` | 结果页可读性样式 |
| `app/static/compare_results_batch.css` | 批量任务面板样式 |
| `app/static/offline_replay.css` | Replay 专用样式 |
| `app/static/app.css` | 仍包含早期结果页布局，修改时避免影响 `/compare` |

现有页面已经具备工作簿导航、Sheet 导航、行差异、字段详情、批量进度、取消/重试、
partial 数据保留和三种模式复用。后续应基于用户指定的问题调整呈现，不重建链路。

## 6. 冻结契约

- 不修改 `m2.diff.v1` 和 `m2.batch.v1`；
- 不建立第二套前端 Diff JSON，可保留临时 view model；
- `source=left`、`target=right`，不得按时间改写为 old/new；
- 使用服务端 summary，不重新推导业务统计；
- partial 同时显示可用 Sheet 和结构化错误；
- failed、网络错误、未执行不能显示成无差异；
- source_only/target_only 行展示真实字段值；
- 当前契约没有 Excel 列字母，不伪造 A1 地址；
- 单侧工作簿和读取失败候选保持明确不可执行/跳过状态；
- 正式页不依赖 Demo 假数据或本地上传；
- Replay 不访问 SVN，不写批量数据库；
- 不修改解析、主键、字段身份、Revision 冻结或 CSV 配对语义；
- 不执行 Excel 写回、Merge 或任何 SVN 写操作。

## 7. 可选改造方向

以下只是问题清单，不构成自动实施授权。用户会在下一对话逐项选择：

- 任务级统计、筛选和当前选择的层级；
- 55 个工作簿下的搜索、状态筛选和定位；
- Sheet 状态、差异数量和错误表达；
- 行与字段的左右值比较方式；
- 长字符串、多行文本、数组、空值的展示；
- partial/error 在保留数据时的可行动提示；
- 大 Sheet 的分批渲染、滚动和选择稳定性；
- 桌面和窄屏下的布局、折叠与溢出；
- 键盘焦点、ARIA 和非颜色状态表达。

结果页是策划反复使用的操作工具，应保持安静、紧凑、便于扫描。不要做营销式布局、
卡片瀑布流或卡片嵌套。

## 8. 每项改造的固定流程

用户每指定一项后：

1. 从 Replay 或固定样本选择代表工作簿；
2. 展示当前页面行为和对应数据形状；
3. 判断问题属于信息架构、渲染、交互、性能还是响应式；
4. 给出最小方案、影响文件和回归风险；
5. 定义桌面/窄屏与状态验收用例；
6. 等待用户明确确认；
7. 实施后运行针对性测试和全量测试；
8. 在正式、Replay、Demo 中检查受影响模式；
9. 报告实际变化，不顺带实施下一项。

## 9. 默认文件边界

允许在用户明确指定后修改：

- `app/templates/compare_results.html`；
- `app/static/compare_results*.js`；
- `app/static/compare_results*.css`；
- `app/static/m2_diff_mapper.js`，仅在 view model 确有需要时；
- `app/static/offline_replay.*`，仅为共享视图兼容；
- `tests/contract/test_compare_preview.py`；
- `tests/contract/test_diff_web_mapping.py`；
- 新增的纯前端或浏览器验收测试。

默认冻结：

- `core/**`；
- `app/schemas/diff.py`；
- `app/schemas/batch.py`；
- `app/services/workbook_diff_service.py`；
- `app/services/batch_diff_service.py`；
- `app/services/workbook_dataset_service.py`；
- 正式批量 API 和 Replay 夹具格式。

若前端需求确实要求后端契约变化，必须单独说明理由和版本影响，等待用户决定。

## 10. 验收参考

每项按影响范围选择，不要求一次完成全部：

- 55 个工作簿可稳定切换；
- 当前工作簿和 Sheet 选择明确；
- modified、unchanged、partial、failed、skipped 和运行中状态可区分；
- `ShopConfig/RefreshShop` 的字段修改无需猜测即可读懂；
- AtlasConfig 大结果操作无明显卡顿；
- source_only/target_only 左右空缺表达一致；
- 长文本可查看完整值且不遮挡相邻内容；
- partial 保留成功 Sheet，错误可定位到 side/sheet/file/code；
- source/target 分支名和 Revision 可见；
- 取消和重试只在合法状态启用；
- 颜色之外保留状态文字；
- 1440x900、1280x800、390x844 下无不可解释重叠；
- 正式模式无 Demo/Replay 数据依赖；
- 页面不发送 SVN 写请求，不修改原始文件。

## 11. 测试与启动

全量基线：

```powershell
py -3 -m pytest -q
```

针对性测试：

```powershell
py -3 -m pytest -q `
  tests/contract/test_compare_preview.py `
  tests/contract/test_diff_web_mapping.py `
  tests/unit/test_offline_fixture.py
```

JavaScript 语法检查：

```powershell
node --check app/static/m2_diff_mapper.js
node --check app/static/compare_results.js
node --check app/static/compare_results_batch.js
node --check app/static/offline_replay.js
```

开发服务：`py -3 -m app.main`。

## 12. M2 完成条件

- 用户指定的 M2-08 前端改造逐项完成并通过验收；
- 自动化基线不退化，新增测试覆盖确认后的交互；
- 正式任务 Task ID、两侧 Revision、候选和终态形成审计记录；
- `MainActivity` 大小写 CSV 在正式任务中不再产生 `M2_CSV_MISSING`；
- 输入完整的最终离线夹具完成评审并达到 55/55/0；
- 路线图和文档目录更新为 M2 完成；
- SVN 只读、无 Merge/写回边界保持不变。

## 13. 下一对话启动提示词

```text
继续 M2-08，开始“行与字段差异”模块逐项改造，仍属于 M2。

工作目录：D:\Excel_Merge

先完整读取：
1. docs/M2-08-ROW-FIELD-DIFF-HANDOFF.md
2. docs/M2-08-DIFF-RESULT-FRONTEND-HANDOFF.md
3. docs/M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md
4. docs/M2-OFFLINE-FIXTURE-RUNBOOK.md
5. 当前 compare_results 模板、JS、CSS、mapper 和对应测试

先恢复上下文并等待我指定第一项行与字段问题。不要自主开始设计或修改代码。

Batch Task、工作簿导航、比对结果标题区、Sheet 标题与导航已经锁定，不得修改。

每一项先使用真实 Replay 数据展示当前行为、代表样本、拟议方案、影响文件和验收
用例，等待我明确确认后再实施。只修改我确认的这一项，不顺带扩大改造范围。

保持 m2.diff.v1、m2.batch.v1、source=left/target=right、冻结 Revision、SVN 只读
和无 Merge/写回语义。不要修改 core 解析与 Diff 规则，不新增第二套前端 Diff
JSON，不把正式结果页接到 Demo 假数据。
```

## 14. M2-08 归档结论

M2-08 用户指定的前端改造已经逐项完成并通过验收，当前页面结构和交互转为冻结基线。
行与字段模块最终实现和真实 Replay 证据见
`archive/M2-08-ROW-FIELD-DIFF-ARCHIVE.md`。

本归档不等于整个 M2 已完成。M2 仍需补齐正式任务 Task ID、冻结 Revision、候选与
终态审计记录，并在输入完整的最终离线夹具上完成评审。上述事项完成前，路线图继续
停留在 M2，不进入 M3。
