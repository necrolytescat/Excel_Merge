# M2-05 阶段 D2：真实双分支试跑报告

> 状态：D2 编排与 Web 链路验收通过；真实数据兼容性问题转入 D3
> 试跑日期：2026-08-06
> 当前自动化基线：`189 passed, 1177 warnings in 10.59s`

## 1. 结论

真实 SVN 双分支的批量流程已经完整跑通：冻结 Revision、服务端生成候选、54 个工作簿逐项执行、状态持久化、失败隔离、结果页轮询和 `result_ref` 按需读取均正常。

本轮 29 个业务失败不是批量编排失败，也不能直接认定为 29 个 Diff 引擎缺陷。它们集中在五类真实数据结构问题，必须在 D3 先分类为“配对/解析缺陷、合法数据变体、源数据不完整”，再决定修复或保留错误。

## 2. 冻结输入

| 项目 | 值 |
|---|---|
| `task_id` | `6131d91a-07b0-4820-a09e-2812e041a3ea` |
| `request_id` | `e3f73b21-ad33-450c-9041-b5fe5acc4d04` |
| source | `KR_FIX_KR-Fix-1.0.0.0@26421` |
| target | `KR_FIX_KR-Fix-1.0.1.0@26421` |
| 候选范围 | `all` |
| 候选指纹 | `e39acba6b27f83b9e8e36427d8ed010848454479e7a3f8424155ee133ca38f63` |
| 创建时间 | `2026-08-06T03:28:03.765043Z` |
| 完成时间 | `2026-08-06T03:32:58.350412Z` |
| 任务终态 | `completed_with_failures` |

工作区当前没有 `.git` 元数据，无法记录可信提交号。因此本轮使用任务 ID、精确 Revision、候选指纹、结果 SHA-256 和测试基线作为复现锚点。

## 3. 结果汇总

| 口径 | 数量 |
|---|---:|
| 候选/已处理 | 54 / 54 |
| 成功 | 25 |
| `modified` | 17 |
| `unchanged` | 8 |
| 业务失败 | 29 |
| `partial` | 18 |
| `failed` | 11 |
| 编排失败 | 0 |
| 业务错误明细 | 166 |

页面复验结果：54 个工作簿均进入终态，没有残留“处理中”；29 个失败项均显示错误数量，点击失败项可通过 `result_ref` 读取真实错误；浏览器控制台无错误。

## 4. 失败分布

| 错误码 | 错误数 | 工作簿数 | 代表样本 |
|---|---:|---:|---|
| `M2_CSV_MISSING` | 88 | 7 | `AttributeConfig.xlsm / Base / source` |
| `M2_CSV_PRIMARY_KEY_MISSING` | 50 | 13 | `ActivityBossConfigNew.xlsm / Base / source` |
| `M2_MANIFEST_FIELD_MISSING` | 12 | 6 | `AreaClean.xlsm / main / source / row 7` |
| `M2_CSV_DUPLICATE_FIELD` | 10 | 5 | `ArenaPeak.xlsm / Map / source / Name` |
| `M2_CSV_STRUCTURE_INVALID` | 6 | 3 | `CalamityLines.xlsm / BossLevelReward / source / column 3` |

受影响工作簿存在交叉，五行工作簿数不能相加作为失败总数。典型风险如下：

- `M2_CSV_MISSING` 的代表文件名实际是 Excel 公式文本，可能涉及公式缓存值读取或导出标记/路径解析，尚未完成根因确认；
- 缺少 `Id/id` 可能是合法的其他主键、复合主键、非数据 Sheet，也可能是源数据缺陷，禁止直接按行号兜底；
- `main` 缺字段可能来自空行、说明行或非导出行，也可能是真实坏数据；
- 重复字段和中间空字段可能是合法布局变体，也可能造成字段身份歧义，不能静默忽略。

## 5. 证据与保留期

机器可读证据：`docs/evidence/M2-05-D2-REAL-TRIAL-6131d91a.json`。

该 JSON 固定了：

- 54 个候选的路径、两侧哈希、候选指纹和终态；
- 54 个 `result_ref`、结果 SHA-256、字节数和内部相对文件名；
- 29 个失败工作簿的全部 166 条原始错误；
- 五类错误统计和代表错误；
- 导出时对全部 gzip 结果的 SHA-256、字节数、Schema 和错误计数校验结果。

校验结果为 `validation_issue_count=0`。原始 SQLite 和 gzip 结果位于 `var/m2-batch`，契约保留期至 `2026-09-05T03:32:58.350412Z`；即使到期清理，文档证据仍保留本轮索引和错误明细，但不包含成功工作簿的完整行级 Diff。

正式服务日志：

- `.cache/formal-5566-20260806-112427.out.log`
- `.cache/formal-5566-20260806-112427.err.log`

`err.log` 仅包含 Uvicorn 正常启动信息，没有异常堆栈。

## 6. 复现与回归规则

重新生成证据：

```powershell
py -3 docs/verify/export_d2_real_trial.py `
  --task-id 6131d91a-07b0-4820-a09e-2812e041a3ea `
  --state-dir var/m2-batch `
  --output docs/evidence/M2-05-D2-REAL-TRIAL-6131d91a.json
```

分析 D3 问题时无需反复执行 54 工作簿全量任务。每完成一类兼容性修复后先跑对应固定夹具和全量 pytest；阶段验收时再以同一端点和 Revision 重跑 54 工作簿，比较成功数、失败码和既有成功项是否回归。

## 7. 后续边界

D3 只处理真实数据兼容性分类和 Diff 引擎加固。不修改 `m2.batch.v1`、批量调度、`source=left/target=right` 方向语义或 SVN 只读边界，不接 Merge/写回。

详细启动约束见 `M2-05-STAGE-D3-COMPATIBILITY-HANDOFF.md`。
