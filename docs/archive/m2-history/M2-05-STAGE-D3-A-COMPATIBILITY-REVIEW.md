# M2-05 阶段 D3-A：真实数据兼容性分类冻结

> 状态：已评审；D3-B 已完成，等待 D3-C 真实回归评审  
> 调查日期：2026-08-06  
> 冻结输入：`KR_FIX_KR-Fix-1.0.0.0@26421` → `KR_FIX_KR-Fix-1.0.1.0@26421`  
> 任务证据：`6131d91a-07b0-4820-a09e-2812e041a3ea`  
> 自动化基线：`189 passed, 1177 warnings in 8.13s`

## 1. D3-A 结论

五个错误码不能按错误码整体“放宽”。`M2_CSV_MISSING` 包含三种根因，
`M2_MANIFEST_FIELD_MISSING` 包含两种根因；主键错误也必须把“键名合法变体”与
“没有可证明身份”分开处理。

本轮只读检查了证据中点名的 Excel/CSV；SVN 操作仅为同端点、同冻结 Revision 的
精确 `list/cat`，没有扫描整个 `TableCsv`，没有写 SVN 或修改原始文件。

| 错误码 | 代表样本与原始结构 | 当前失败点 | 分类冻结 | D3-B 处置边界 |
|---|---|---|---|---|
| `M2_CSV_MISSING`（88） | `AttributeConfig/main/row 2`：`A2=Base`；`B2` 为公式，缓存值 `AttributeConfig_Base`；`C2=1`。`HeroConfig` 的 `_xlfn.CONCAT` 公式也有正确缓存值。 | `openpyxl` 使用 `data_only=False`，公式文本被当成 `tbxName`；此外当前契约不排除 `isExport=0` 行。 | **84 条：1 配对/解析缺陷**。`QuestConfig/Statement` 两侧 `isExport=0`，共 **2 条：2 合法非导出变体**。`MainActivity/FunctionName` 两侧 `isExport=1` 且精确路径不存在，共 **2 条：3 源数据缺陷**。 | 修缓存值读取，并在 CSV 读取前排除明确非导出行；真正导出文件缺失继续 `M2_CSV_MISSING`。禁止公式求值、文件名推导和目录猜测。 |
| `M2_CSV_PRIMARY_KEY_MISSING`（50） | `ActivityBossConfigNew_Base.csv` 第 2 行首字段为 `ID`，24 行数据均非空且唯一，两侧键集合相同。`ArenaTop64_Notice.csv` 第 23 条记录只有 `scope=None` 的 `Des=-3`，全部业务字段为空。 | 主键只精确接受 `Id/id`；数据行“全空”判断发生在非业务列过滤之前。 | 代表样本为 **2 合法数据变体**：唯一大小写变体 `ID`，以及仅注释列有值的非业务行。其余没有唯一大小写匹配的 Sheet 不在本轮推断主键。 | 可提议唯一 casefold 匹配和按业务列判空；禁止首列、行号、内容哈希、复合键或工作簿名兜底。无可证明键时继续报错。 |
| `M2_MANIFEST_FIELD_MISSING`（12） | `AreaClean/row 7` 在表 `A1:F8` 内：`sheetName=ConditionIllustration`，`tbxName/isExport` 空；`SystemConfig/row 4`：`sheetName=Condition`、公式缓存空、`isExport=0`。`CharacterConfig/row 10` 位于表 `A1:F6` 外；`ImpactConfig/row 19` 位于表 `A1:F13` 外。 | 解析器从表头扫描到工作表末尾，不使用 Excel Table 的 `ref`；表内又把非导出/空公式行当成残缺导出记录。 | 表外两工作簿共 4 条为 **1 解析边界缺陷**；表内非导出或空行共 8 条为 **2 合法清单变体**。 | 先按唯一 manifest Table 范围截断，再按缓存值和导出状态判定行资格。真正导出行缺字段仍返回原错误。 |
| `M2_CSV_DUPLICATE_FIELD`（10） | `ArenaPeak_Map.csv`：列 5 `Name/string/Client`，列 8 `Name/string/None`；`MainActivity_GroupPack.csv` 是 `Type/None` 与 `Type/All`。其余 3 个样本的重复项均为 `scope=None`。 | 在解释第 4 行 scope 前，直接对第 2 行所有代码名做唯一性校验。 | **2 合法数据变体**。五个样本过滤明确的非业务 `scope=None` 后，没有活跃业务字段身份冲突。 | 只允许明确 `scope=None` 的列进入 `non_business`；活跃列仍重复时继续原错误。不得按出现序号给两个活跃同名字段造身份。 |
| `M2_CSV_STRUCTURE_INVALID`（6） | `CalamityLines_BossLevelReward.csv` 第 3 列：显示名“备注”，代码名/类型/scope 空，240 行注释有值；另两例也是带显示名的空代码注释列。 | 最后一个命名字段前出现空代码名即失败，没有识别非业务注释列。 | **2 合法数据变体**。三例两侧结构一致，均有显示名且无有效业务代码、类型和 scope。 | 只识别满足窄结构约束的注释列；声明类型或活跃 scope 的空代码列继续失败。物理列位置必须保留。 |

## 2. 代表原始结构

以下内容来自 Revision `26421` 的两侧原始文件；除特别说明外，两侧结构相同。

### 2.1 公式值、导出标记与缺文件

```text
AttributeConfig.xlsm / main / row 2
A2 cached="Base"
B2 formula=IF(...); cached="AttributeConfig_Base"
C2 cached="1"

HeroConfig.xlsm / main / row 2
A2 cached="Global"
B2 formula=IF(C2=1,_xlfn.CONCAT("HeroConfig_",A2),"")
   cached="HeroConfig_Global"
C2 cached="1"

MainActivity.xlsm / FunctionName / row 3
tbxName cached="MainActivity_FunctionName"; isExport="1"
两侧 MainActivity_FunctionName.csv 均不存在

QuestConfig.xlsm / Statement / row 21
tbxName="QuestConfig_Statement"; isExport="0"
两侧 QuestConfig_Statement.csv 均不存在
```

前两例不需要 Excel 公式计算器，原文件已经提供缓存结果。`MainActivity/FunctionName` 是
真正导出文件缺失；`QuestConfig/Statement` 是当前引擎错误读取了非导出项。

### 2.2 大小写主键变体

```text
ActivityBossConfigNew_Base.csv
第 2 行: ID,bz,MissionID,PointCoefficient,...
数据区: 24 行
ID: 非空 24，唯一 24
两侧: source-only ID 0，target-only ID 0
```

`ID` 是 `Id/id` 的唯一大小写变体，证据足以支持窄 casefold 规则；不能据此授权
`MissionID`、`RankID`、任意唯一列或复合列成为主键。

### 2.3 manifest 表范围与非导出行

| 工作簿 | 错误行 | manifest Table `ref` | 原始行摘要 | 判断 |
|---|---:|---|---|---|
| `AreaClean.xlsm` | 7 | `A1:F8` | `A=ConditionIllustration`，B/C 空 | 表内非导出行 |
| `HotRecommend.xlsm` | 10 | `A1:F10` | `A=Notes`，B/C 空 | 表内非导出行 |
| `SpineConfig.xlsm` | 3 | `A1:F4` | 仅 B 有公式，缓存为空 | 表内空公式行 |
| `SystemConfig.xlsm` | 4 | `A1:F7` | `A=Condition`，B 缓存空，`C=0` | 表内显式非导出行 |
| `CharacterConfig.xlsm` | 10 | `A1:F6` | 从 B 列开始的辅助内容 | 表外越界扫描 |
| `ImpactConfig.xlsm` | 19 | `A1:F13` | B–E 为 `LogicID/Desc/ViewID/Desc` | 表外越界扫描 |

### 2.4 重复字段身份

| 文件 | 重复字段 | 发生列及 scope | 活跃业务重复数 |
|---|---|---|---:|
| `ArenaPeak_Map.csv` | `Name` | 5=`Client`，8=`None` | 1 |
| `ConditionConfig_Target.csv` | `Tip` | 3=`None`，6=`None` | 0 |
| `DropNewConfig_Base.csv` | `Mark` | 5=`None`，6=`None` | 0 |
| `MainActivity_GroupPack.csv` | `Type` | 6=`None`，7=`All` | 1 |
| `Monster_Base.csv` | `Desc` | 2=`None`，3=`None` | 0 |

这不授权“同名字段按第几次出现”作为身份。过滤 `scope=None` 后仍有两个活跃同名字段时，
数据仍不可无歧义比较。

### 2.5 中间注释列

| 文件 | 列 | 显示名 | 代码名 | 类型 | scope | 数据区非空 |
|---|---:|---|---|---|---|---:|
| `CalamityLines_BossLevelReward.csv` | 3 | 备注 | 空 | 空 | 空 | 240/240 |
| `GameConfig_Mail.csv` | 14 | 参数描述 | 空 | 空 | `None` | 23/386 |
| `HeroConfig_ReburnAwake.csv` | 8 | 备注 | 空 | 空 | 空 | 10/10 |

这些列是策划注释列，不是全空间隔列。规则必须保留物理列位置并明确记录
`non_business` 身份，不能删除单元格后让右侧字段左移。

## 3. 拟议兼容规则

以下规则是 D3-B 候选，评审前不实施。

### R1. manifest 公式值读取

1. `sheetName/tbxName/isExport` 的语义值读取公式缓存结果，不把公式文本当值。
2. 不执行、解释或模式匹配 Excel 公式，也不从工作簿名和 `sheetName` 重建 `tbxName`。
3. 导出记录的 `tbxName` 缓存为空时返回稳定 `M2_MANIFEST_FIELD_MISSING`，附带行号和字段名。
4. 缓存值仍按单一文件名校验；禁止路径分隔符和目录穿越。

### R2. manifest Table 边界与行资格

1. 若 `main` 存在唯一一个表头含 `sheetName/tbxName` 的 Excel Table，只解析其 `ref`。
2. Table 外内容不参与 manifest；多个候选 Table 仍视为清单歧义。
3. 对 Table 内行先应用 R1，再判断：
   - `sheetName/tbxName` 均空：空行，跳过；
   - 两者均非空且 `isExport` 不为显式 false：有效记录；
   - `tbxName` 为空且 `isExport` 为 false 或空：非导出行，记录分类后跳过；
   - `isExport` 为 true 但任一必需字段为空：保持字段缺失错误；
   - 只有 `tbxName`，或 `isExport=false` 但 `tbxName` 非空：保持结构错误，不猜测。
4. 没有 Excel Table 的最小/历史工作簿可保留唯一表头扫描兼容路径，但不得越过第二个独立内容区。

`QuestConfig/Statement` 表明“`isExport=false` 但 `tbxName` 非空”在真实数据中是非导出项，
因此上面最后一条需要评审为以下二选一：以 `isExport` 为权威并跳过，或继续报结构错误。
本报告建议以 `isExport` 为权威并保留内部诊断，不读取 CSV。

R2 会修订“`isExport` 不排除 Sheet”的旧约定，必须作为显式契约变更评审。

### R3. 业务列与非业务列

1. `scope` 大小写无关等于 `None` 的命名列归类为 `non_business`。
2. 非业务列在重复字段校验、业务空行判断、主键和 Diff 之前剔除，但保留物理位置、显示名、
   代码名、scope 和忽略原因的内部诊断。
3. 过滤后仍有活跃同名字段，继续返回 `M2_CSV_DUPLICATE_FIELD`。
4. 未知/空 scope 的命名字段不自动判为非业务字段。

### R4. 空代码名注释列

仅当中间列同时满足以下条件时，归类为 `non_business_annotation`：

- 第 1 行显示名非空；
- 第 2 行代码名为空；
- 第 3 行类型为空；
- 第 4 行 scope 为空或 `None`。

该列可以有注释值，但不参与业务 Diff。任一条件不满足时继续
`M2_CSV_STRUCTURE_INVALID`。本轮不扩展到任意空白间隔列。

### R5. 主键大小写兼容

1. 仍先按配置顺序精确查找 `Id`、`id`。
2. 精确查找失败时，只允许对配置候选执行 casefold，并且必须恰好命中一个物理字段。
3. 输出中的 `primary_key` 保留原始代码名，例如 `ID`；键值仍是不透明原始字符串。
4. 同时存在 `Id/ID` 等多个 casefold 命中时返回稳定歧义错误。
5. 禁止首列、任意唯一列、行号、相邻行、内容哈希、工作簿名规则和隐式复合键兜底。

### R6. 缺文件保持失败

通过 R2 判定为有效导出记录后，`tbxName` 对应精确路径不存在时继续
`M2_CSV_MISSING`。不得扫描整个 `TableCsv`，也不得因为两侧都缺失而判定无差异。
明确非导出记录不得进入 CSV 读取阶段。

## 4. 固定回归夹具方案

D3-B 建议新增 `tests/fixtures/d3_compat/`，只保留从真实结构缩减得到的最小派生夹具，
不复制完整生产工作簿或大 CSV。`fixture_manifest.json` 记录来源工作簿/Sheet、Revision、
结构摘要、期望分类和夹具 SHA-256。

| 夹具 | 最小内容 | 期望 |
|---|---|---|
| `manifest_formula_cached.xlsm` | `main` Table；`tbxName` 有公式与缓存，`isExport=1` | 使用缓存值，不出现公式文件名 |
| `manifest_formula_no_cache.xlsm` | 同上但导出行缓存为空 | 字段缺失；不得计算公式或推导文件名 |
| `manifest_table_bounds.xlsm` | Table `A1:F4`，第 6 行放辅助文本 | 第 6 行不进入清单 |
| `manifest_non_export_rows.xlsm` | 完整导出行、`isExport=0` 行、空公式缓存行 | 只保留完整导出行 |
| `uppercase_id.csv` | 第 2 行 `ID,Name`；键 `001/2` | 主键 `ID`，保留原始键字符串 |
| `ambiguous_case_id.csv` | 同时包含 `Id` 与 `ID` | 稳定主键歧义错误 |
| `scope_none_duplicate.csv` | `Name/Client` 与 `Name/None` | 只比较 Client 列，保留忽略诊断 |
| `active_duplicate.csv` | 两个 `Name/All` | 继续重复字段错误 |
| `middle_annotation.csv` | `Id,(备注空代码),Value`，备注有值 | Id/Value 位置和值正确，备注不参与 Diff |
| `invalid_middle_field.csv` | 空代码列但声明 `uint32/All` | 继续结构错误 |
| `annotation_only_tail_row.csv` | 末行仅 `scope=None` 列有值 | 按业务列为空跳过，不触发空主键 |
| `missing_exported_literal_csv` | `isExport=1`，字面量文件不存在 | 左右侧各保留 `M2_CSV_MISSING` |
| `missing_non_export_literal_csv` | `isExport=0`，字面量文件不存在 | 不读取 CSV，不产生缺文件错误 |

夹具不得加入真实端点 URL、认证信息、绝对路径或生成时间。CSV 保留 BOM、逻辑记录号和物理
列宽；XLSM 夹具必须包含真实 Table 关系和公式缓存节点，不能只用 mock 返回值代替。

## 5. 拟议验收用例

### 5.1 manifest 解析

- `test_manifest_uses_cached_formula_value`
- `test_manifest_does_not_evaluate_formula_without_cache`
- `test_manifest_uses_unique_table_ref_as_row_boundary`
- `test_manifest_skips_non_export_row_with_empty_or_stale_tbx`
- `test_manifest_rejects_exported_row_missing_tbx`
- `test_manifest_rejects_multiple_candidate_tables`

### 5.2 CSV 解析与主键

- `test_parser_accepts_unique_casefold_primary_key`
- `test_parser_rejects_ambiguous_casefold_primary_keys`
- `test_parser_never_infers_unconfigured_or_composite_key`
- `test_parser_filters_scope_none_before_duplicate_validation`
- `test_parser_keeps_active_duplicate_field_failure`
- `test_parser_recognizes_labeled_middle_annotation_column`
- `test_parser_keeps_physical_positions_after_ignored_column`
- `test_parser_rejects_empty_code_with_active_scope_or_type`
- `test_parser_skips_row_with_only_non_business_values`

### 5.3 服务与契约保护

- 公式缓存成功后，`source_csv.name/target_csv.name` 为字面量文件名，不含 `=`、`IF(` 或路径。
- 有效导出记录的字面量 CSV 缺失时仍为 Sheet 失败；非导出记录不读 CSV。
- `source=left/target=right`、`source_only/target_only` 和原始键字符串语义不变。
- AtlasConfig 固定真值和规范 JSON SHA-256 不退化；若新增兼容诊断，先评审
  `m2.diff.v1` 向后兼容性。
- `py -3 -m pytest -q` 不低于 `189 passed`，无新增非预期 warning。

## 6. 评审门禁

D3-B 开始前至少确认：

1. 是否接受 R2 对旧 `isExport` 约定的窄修订，并以导出标记为权威；
2. 是否接受 `scope=None` 和 R4 注释列作为明确的非业务列身份；
3. 是否接受唯一 casefold `ID`，并继续拒绝所有未经配置的单键/复合键。

评审前不修改 `core/workbook_manifest_parser.py`、`core/table_csv_parser.py`、
`core/semantic_diff.py` 或主键配置。本阶段不运行真实回归任务，不修改批量调度和页面，
不接 Merge/写回，也不执行 SVN 写操作。
