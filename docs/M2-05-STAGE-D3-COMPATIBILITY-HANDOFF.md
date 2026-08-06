# M2-05 阶段 D3：真实数据兼容性加固交接

> 状态：D3-C 真实回归与离线夹具已完成
> 更新日期：2026-08-06
> 输入基线：D2 真实任务 `6131d91a-07b0-4820-a09e-2812e041a3ea`
> 当前自动化：`205 passed, 1298 warnings in 14.95s`

## 1. 目标

D3 的目标不是追求“54 个工作簿全部变绿”，而是让引擎正确覆盖合法真实数据变体，同时继续明确报告源数据缺陷。每一类当前失败都必须先得到可解释分类，再修改规则。

分类只有三种：

1. 路径、配对、公式值读取或导出标记处理错误；
2. 合法数据变体，当前解析器或主键策略覆盖不足；
3. 源数据确实不完整或存在歧义，应继续返回稳定业务错误。

## 2. 必读输入

1. `docs/M2-05-STAGE-D2-REAL-DATA-TRIAL-REPORT.md`；
2. `docs/evidence/M2-05-D2-REAL-TRIAL-6131d91a.json`；
3. `docs/M2-02-TO-M2-04-STABLE-DIFF-JSON-PLAN.md`；
4. `docs/adr/ADR-007-m2-table-tablecsv-pairing.md`；
5. `core/workbook_manifest_parser.py`、`core/table_csv_parser.py`、`core/semantic_diff.py` 和 `core/m2_errors.py`。

原始 `m2.diff.v1` 在保留期内位于 `var/m2-batch/results/6131d91a-07b0-4820-a09e-2812e041a3ea`，到期时间为 `2026-09-05T03:32:58.350412Z`。

## 3. 当前失败基线

| 优先级 | 错误码 | 错误数/工作簿数 | 首个调查点 |
|---:|---|---:|---|
| 1 | `M2_CSV_MISSING` | 88 / 7 | 公式文本、缓存值、导出标记和 CSV 路径是否被正确解析 |
| 2 | `M2_CSV_PRIMARY_KEY_MISSING` | 50 / 13 | 实际业务键、复合键、无序集合或非数据 Sheet 的判定依据 |
| 3 | `M2_MANIFEST_FIELD_MISSING` | 12 / 6 | `main` 空行、说明行、非导出行和坏数据的区分 |
| 4 | `M2_CSV_DUPLICATE_FIELD` | 10 / 5 | 重复字段是否具有可证明的稳定身份，还是本身不可比较 |
| 5 | `M2_CSV_STRUCTURE_INVALID` | 6 / 3 | 中间空表头是合法间隔列还是破坏业务字段结构 |

优先级表示调查顺序，不预先代表修复结论。相同工作簿可能命中多类错误。

## 4. 执行顺序

### D3-A：分类冻结

- 每类至少选择一个代表工作簿，记录 Sheet、左右侧、文件名、原始表头或 `main` 行结构；
- 查明两侧是否采用相同结构，以及当前解析器在哪个判断点失败；
- 为每个样本给出三类归属和证据；
- 先输出分类表和拟议规则，涉及主键或字段身份时等待评审，不立即扩大语义。

### D3-B：逐类加固

- 先建立最小固定夹具和失败测试，再实施单类规则；
- 规则必须是可判定的结构约束，不能基于工作簿名硬编码；
- 对仍属源数据缺陷的样本，保持业务失败，并改善错误信息或细节字段（仅在兼容契约允许时）；
- 每类完成后运行针对性测试和全量测试，再更新错误基线。

### D3-C：真实回归

- 通过现有 Web 端使用相同 source/target 创建新任务，由系统在任务创建时解析并冻结当次 Revision；记录实际 Revision，不新增固定 Revision 输入；
- 与本轮候选指纹和 54 项索引比较，候选变化必须先解释；
- 确认原 25 个成功项没有退化；
- 对减少的每一类错误给出对应规则和测试依据；
- 保留仍被判定为源数据缺陷的错误，不以“全绿”为验收条件。

## 5. 禁止事项

- 禁止把缺少 `Id/id` 直接降级为按行号比较；行号不能证明业务实体身份；
- 禁止静默丢弃重复字段、空字段或缺失 `main` 字段；
- 禁止扫描整个 `TableCsv` 猜测工作簿归属；
- 禁止重新读取 `HEAD`，Excel 与 CSV 必须来自各自端点的冻结 Revision；
- 禁止交换 `source=left`、`target=right` 或改变 `m2.diff.v1` 方向语义；
- 禁止修改 SVN、原始 Excel/CSV，或接入 Merge/写回；
- 禁止在 D3 重做批量调度器和页面运行时。

## 6. 验收条件

- 每一类错误均有代表夹具、分类结论和明确规则；
- 合法变体的修复有单元/契约测试，源数据缺陷仍有稳定错误测试；
- `py -3 -m pytest -q` 不低于当前 `205 passed`，无新增非预期警告；
- 真实回归全部候选进入终态；本轮为 55/55，编排失败为 0；
- 原 25 个成功工作簿不退化为业务失败；
- 新任务的结果统计与本报告基线形成可审计差异；
- 不违反 SVN 只读、冻结 Revision、方向语义和无行号兜底约束。

## 7. 对话启动提示词

### 7.1 D3-A 历史提示词

以下提示词对应已完成的 D3-A，仅保留用于审计，不再作为下一阶段入口。

```text
执行 docs/M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md 的 D3-A。

先读取 D2 真实试跑报告和机器可读证据，运行 py -3 -m pytest -q
确认 189 passed 基线。针对五类真实失败提取代表样本的原始结构，
分别判断为配对/解析缺陷、合法数据变体或源数据缺陷。

本轮先输出分类表、固定回归夹具方案、拟议兼容规则和验收用例。
涉及主键、复合键、重复字段身份或行号兜底时不得直接修改语义；
禁止 SVN 写操作，不修改批量调度和页面，不接 Merge/写回。
D3-A 完成后停止等待评审。
```

### 7.2 Replay 数据矫正提示词

```text
开展下一阶段离线数据矫正。

先读取：
- docs/M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md
- docs/M2-OFFLINE-FIXTURE-RUNBOOK.md
- 与本次问题相关的解析、配对、语义 Diff 实现和测试

使用夹具：
D:\Excel_Merge\var\m2-fixtures\d3c-6e501824.m2fixture
Task：6e501824-ac7d-49d4-bd7f-6d7136a958f1
Revision：source/target 均为 26438

先运行 py -3 -m pytest -q，确认不低于 205 passed。随后在开发模式启动
/compare/replay，加载夹具并执行“重算全部”，确认初始基线为 55 current、
55 matched、0 mismatched。不得重新访问 SVN 来构造本轮离线输入，也不得在
加载或重算时重新生成夹具、自动覆盖黄金 m2.diff.v1。

按用户指定的工作簿或问题逐项处理。每项先从夹具提取原始 Excel main 清单、
Table 范围、相关 CSV 表头/类型/scope/数据行和当前 Diff，判断属于：
1. 配对或解析缺陷；
2. 合法数据变体；
3. 源数据缺陷。

在修改代码前，先输出代表样本、失败原因、拟议兼容规则、影响范围、固定回归
夹具方案和验收用例，等待评审确认。已确认语义继续保持：只有 isExport=1
才要求并配对 CSV；第四行值为 None 的字段不参与对比。

规则获批后再做最小范围实现，运行针对性测试和全量测试；如修改了解析或 Diff
代码，重启服务、重新加载同一夹具并重算，报告 matched/mismatched 变化及每个
不一致项。预期业务语义发生变化时，也必须先评审差异，再显式更新黄金基线。

不得擅自修改主键、复合键、重复字段身份或增加行号、首列、哈希兜底；禁止
SVN 写操作、Merge/写回、固定 Revision 页面、正式批量调度和正式页面流程变更。

离线矫正稳定后运行全量自动化测试，最后再通过一次真实 Web 任务验证 SVN
快照、Revision 冻结、候选生成和批量编排链路。单项评审或实现完成后停止，
等待用户指定下一项，不批量扩大兼容语义。
```

## 8. D3-C 真实回归结果

D3-B 规则上线后，Web 端重新创建并完成以下真实任务：

| 项目 | 结果 |
|---|---|
| Task | `6e501824-ac7d-49d4-bd7f-6d7136a958f1` |
| Revision | source/target 均为 `26438` |
| Source | `KR_FIX_KR-Fix-1.0.0.0` |
| Target | `KR_FIX_KR-Fix-1.0.1.0` |
| 候选 | 55 |
| 候选变化 | 相比 D2 新增 `PetRace.xlsm`，无候选移除 |
| 回归保护 | D2 原 25 个 succeeded 全部保持 succeeded |
| 结果 | 51 succeeded，4 business_failed，0 orchestration_failed |
| manifest SHA-256 | `5445434f9c300681da92839572aa11b8f3677573e31600b0aca09d72b1e4b2e5` |

剩余 8 条业务错误来自 4 个工作簿：

- `MainActivity/FunctionName`：双侧精确 CSV 均缺失，共 2 条；
- `HeroConfig/Reborn_TransferLevel`：双侧无可证明主键，共 2 条；
- `PetConfig/ValueQuality`：双侧无可证明主键，共 2 条；
- `ShopConfig/RefreshShop`：双侧无可证明主键，共 2 条。

以上是 D3-C 当时的结果，不是最终分类。2026-08-06 离线复核后，
`MainActivity/FunctionName` 被重新归类为文件名大小写配对缺陷；其 SVN 实际文件为
`MainActivity_FunctIonName.csv`，唯一 `casefold` 完全匹配规则已修复该问题。
另外三个无 `Id/id` Sheet 被归类为合法数据变体，经评审后采用物理第一列业务
字段作为主键；仍未加入行号、任意唯一列、复合键或哈希兜底。

## 9. 离线数据矫正基线

已生成除已知 `MainActivity` CSV 输入缺口外的原始数据与黄金结果夹具：

- 路径：`var/m2-fixtures/d3c-6e501824.m2fixture`；
- SHA-256：`31353872bde2ab53fee8e0d6dfda31196117564361a0d51dfbfc0921487d0380`；
- 内容：726 个原始 Excel/CSV 输入索引、2 个显式缺失项、55 个黄金 `m2.diff.v1`；
- 任务摘要：54 succeeded、1 business_failed；剩余失败仅由旧夹具未保存两侧大小写变体 CSV 字节导致，不是源数据缺陷；
- 离线重算：55 current、55 matched、0 mismatched。

该黄金结果于 2026-08-06 经评审显式纳入第一列主键兜底规则。旧基线保存在
`var/m2-fixtures/d3c-6e501824.pre-first-column-key.m2fixture`，SHA-256 为
`f62564f37f9101c116cf910224f1234bc2869b5df9d269d707e7684e8f509fc0`。

Web 端仅在开发模式开放 `/compare/replay`，支持黄金回放、当前代码全量/单项重算和一致性标记。离线服务不持有 SVN provider 或 `BatchStore`，不访问 SVN、不写批量 SQLite。

详细格式、安全门禁和使用方式见 `docs/M2-OFFLINE-FIXTURE-RUNBOOK.md`。
