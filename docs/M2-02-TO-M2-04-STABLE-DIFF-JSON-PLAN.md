# M2-02 至 M2-04：稳定 Diff JSON 纵向实施计划

> 状态：已完成（2026-08-05）  
> 输入：`tests/excel/left` 与 `tests/excel/right` 的 AtlasConfig Excel+CSV 数据集  
> 终点：对同一输入可重复生成结构稳定、顺序稳定、方向明确的 `m2.diff.v1` JSON  
> 执行边界：本地单工作簿，不接 SVN、不接前端、不做批量文件 Diff

## 1. 大白话目标

给程序一份左侧 AtlasConfig 数据集和一份右侧 AtlasConfig 数据集，程序应：

1. 只读 Excel 的 `main` Sheet，知道有哪些业务 Sheet、每个 Sheet 对应哪个 CSV；
2. 按固定 CSV 规则读取业务数据；
3. 按 `sheetName → Id/id → 字段名` 找出差异；
4. 输出一份稳定 JSON；
5. 相同输入与相同配置重复执行，JSON 字节完全一致。

本阶段完成后，前端和 SVN 都只需要消费或提供数据，不再重新解释 Diff 业务规则。

## 2. 当前代码起点与处理原则

项目已有 `core/csv_parser.py` 和 `core/differ.py`，但不能直接作为正式 M2 契约：

| 现状 | 与 M2 规则的冲突 | 处理 |
|---|---|---|
| `csv_parser.py` 默认第 1 行为表头 | 正式字段名在第 2 行，数据从第 8 行开始 | 新增严格的 TableCsv 解析器 |
| 解析器会删除空白行 | 会改变物理行号，掩盖结构错误 | 保留原始行号，只跳过数据区的完全空行 |
| 解码失败后使用 replacement 字符 | 可能把损坏数据伪装成合法文本 | UTF-8 BOM/UTF-8 失败即返回稳定错误 |
| 通过文件名下划线推断工作簿与 Sheet | 正式映射来自 `main.sheetName/tbxName` | 禁止文件名归属推断 |
| `differ.py` 会用内容哈希和行号兜底配对 | 可能把不同主键的两行误判为修改 | M2 只允许 `Id/id` 精确匹配 |
| 现有引擎遍历 `set` | JSON 中行或字段顺序可能跨进程变化 | 所有输出使用显式稳定顺序 |
| 工作簿级 Sheet 增删计数方向存在混淆 | 容易把 source-only/target-only 写反 | v1 只使用方向明确的状态名 |

`core/csv_parser.py`、`core/differ.py` 暂时保留给现有历史调用方。M2 新链路使用独立的严格模块，验证稳定后再评估是否合并旧实现。

## 3. “稳定 JSON”的定义

`m2.diff.v1` 必须同时满足：

- **结构稳定**：有明确 `schema_version`，Pydantic 模型禁止未声明字段；
- **方向稳定**：只使用 `source/target` 和 `source_only/target_only`，不出现 `old/new`、`added/removed` 歧义；
- **顺序稳定**：Sheet、字段、行、错误均按书面规则排序，不依赖 `set` 或文件系统遍历顺序；
- **值类型稳定**：CSV 原值在 JSON 中统一为字符串，缺少一侧时使用 `null`；
- **序列化稳定**：统一 UTF-8、`ensure_ascii=false`、2 空格缩进、文件末尾一个换行；
- **元数据稳定**：正式结果不包含生成时间、耗时、进程 ID、临时目录或绝对路径；
- **错误稳定**：返回固定错误码和脱敏上下文，不返回 Python 堆栈或平台相关异常文本；
- **可复现**：相同输入文件字节与 `dataset_layout` 配置，两次输出的 SHA-256 相同。

性能和诊断信息可以由调用层单独记录，不能污染正式 Diff JSON。

## 4. 固定处理流程

```text
source/target 本地数据集
→ 计算输入文件 SHA-256
→ 只读解析两侧 Excel main 清单
→ 按 source main 顺序建立逻辑 Sheet 列表
→ 用各侧 tbxName 精确读取 CSV
→ 校验字段、类型、范围、数据区和 Id/id
→ 严格主键 Diff
→ 汇总工作簿、Sheet、行、字段和错误
→ Pydantic 契约校验
→ 规范序列化为 m2.diff.v1 JSON
```

若 source 中不存在、只在 target 中存在的逻辑 Sheet，追加在 source Sheet 之后，并保持 target `main` 中的顺序。

## 5. Diff JSON v1 草案

### 5.1 顶层结构

```json
{
  "schema_version": "m2.diff.v1",
  "direction": {
    "source": "left",
    "target": "right"
  },
  "workbook": {
    "name": "AtlasConfig.xlsm",
    "status": "modified",
    "source_sha256": "<sha256>",
    "target_sha256": "<sha256>"
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

### 5.2 Sheet 结构

```json
{
  "sheet_name": "TeamConfig",
  "status": "modified",
  "primary_key": "Id",
  "source_csv": {
    "name": "AtlasConfig_TeamConfig.csv",
    "sha256": "<sha256>"
  },
  "target_csv": {
    "name": "AtlasConfig_TeamConfig.csv",
    "sha256": "<sha256>"
  },
  "summary": {
    "source_only_rows": 15,
    "target_only_rows": 15,
    "modified_rows": 0,
    "modified_fields": 0
  },
  "fields": [],
  "rows": [],
  "errors": []
}
```

### 5.3 行与字段结构

```json
{
  "key": "1001",
  "status": "modified",
  "source": {
    "row_number": 8,
    "values": {
      "Id": "1001",
      "Name": "SourceName"
    }
  },
  "target": {
    "row_number": 9,
    "values": {
      "Id": "1001",
      "Name": "TargetName"
    }
  },
  "changes": [
    {
      "field": "Name",
      "status": "modified",
      "source": "SourceName",
      "target": "TargetName"
    }
  ]
}
```

完整字段保留在 `source.values` 与 `target.values` 中，字段差异只出现在 `changes` 中。这样页面既能展示整行上下文，也不需要重新计算变化字段。

### 5.4 状态枚举

| 层级 | 允许值 |
|---|---|
| 工作簿 | `unchanged`、`modified`、`partial`、`failed` |
| Sheet | `unchanged`、`modified`、`source_only`、`target_only`、`failed` |
| 行 | `modified`、`source_only`、`target_only` |
| 字段 | `modified`、`source_only`、`target_only` |

`partial` 表示至少一个 Sheet 成功、至少一个 Sheet 失败；`failed` 表示没有可用业务结果。

### 5.5 错误结构

```json
{
  "code": "M2_CSV_DUPLICATE_KEY",
  "stage": "csv_parse",
  "side": "source",
  "workbook": "AtlasConfig.xlsm",
  "sheet_name": "TeamConfig",
  "file": "AtlasConfig_TeamConfig.csv",
  "message": "主键 Id 存在重复值",
  "details": {
    "key": "1001",
    "rows": [8, 12]
  }
}
```

第一版固定错误码：

- `M2_WORKBOOK_PARSE_FAILED`；
- `M2_MANIFEST_SHEET_MISSING`；
- `M2_MANIFEST_FIELD_MISSING`；
- `M2_MANIFEST_DUPLICATE_SHEET`；
- `M2_CSV_MISSING`；
- `M2_CSV_DECODE_FAILED`；
- `M2_CSV_STRUCTURE_INVALID`；
- `M2_CSV_DUPLICATE_FIELD`；
- `M2_CSV_PRIMARY_KEY_MISSING`；
- `M2_CSV_DUPLICATE_KEY`；
- `M2_DIFF_INTERNAL_ERROR`。

## 6. 匹配与排序规则

### 6.1 逻辑 Sheet

- 以 `sheetName` 精确匹配，首版区分大小写；
- `main` 中同时具有非空 `sheetName` 和 `tbxName` 的记录进入比较；
- `isExport` 在 v1 中保留为清单元数据，不单独排除 `sheetName`；固定规则是 `sheetName` 下的业务 Sheet 均有可靠 CSV；
- source Sheet 按 source `main` 顺序输出；
- target-only Sheet 追加在末尾，按 target `main` 顺序输出；
- `tbxName` 只负责各侧 CSV 定位，不参与逻辑 Sheet 身份判断；
- `main` 与 `配置公式2` 不作为业务 Sheet 输出。

### 6.2 字段

- 本文所称 CSV 第 N 行均指标准 `csv.reader` 解析后的第 N 条逻辑记录；引号内换行不增加记录号；
- 字段身份取 CSV 第 2 行代码名，精确匹配；
- 公共字段保持 source 字段顺序；
- target-only 字段追加在公共字段之后，保持 target 字段顺序；
- 字段移动不算差异；
- 第 2 行末尾没有代码名的注释列不参与业务 Diff；业务字段之间的空代码名、重复字段名或数据宽度超过导出布局时返回结构错误。

### 6.3 行

- 主键优先 `Id`，其次 `id`；
- 主键值作为不透明字符串精确匹配，不转数字，不补零；
- 主键变化表现为一条 `source_only` 和一条 `target_only`，不能按行号拼成 `modified`；
- 行重排不算差异；
- 不启用内容哈希、相邻行、模糊相似度或行号兜底；
- 空主键和重复主键返回错误，不继续猜测。

### 6.4 值

- 输出保留 CSV 原始字符串；
- 字符串不 trim；
- 空字段输出空字符串，某一侧整行或字段不存在时输出 `null`；
- 比较值可按第 3 行声明类型生成内部规范值，但不改变输出原值；
- 只支持明确映射的数字、布尔、日期类型；未知类型按字符串精确比较；
- 不根据内容猜测日期或数字，不读取 Excel 显示格式。

## 7. 建议代码落点

| 文件 | 职责 |
|---|---|
| `app/schemas/diff.py` | `m2.diff.v1` Pydantic 模型、状态枚举、错误结构 |
| `core/workbook_manifest_parser.py` | 只读提取 `main` 的 `sheetName/tbxName/isExport` |
| `core/table_csv_parser.py` | 按 `dataset_layout` 严格解析 CSV 元数据与数据区 |
| `core/semantic_diff.py` | 严格按逻辑 Sheet、主键和字段名计算差异 |
| `app/services/workbook_diff_service.py` | 编排两侧数据集、汇总错误、生成契约模型 |
| `app/tools/diff_sample.py` | 本地单工作簿入口，输出规范 JSON |
| `tests/unit/test_workbook_manifest_parser.py` | `main` 清单解析单测 |
| `tests/unit/test_table_csv_parser.py` | CSV 结构、编码、主键和类型单测 |
| `tests/unit/test_semantic_diff.py` | 严格匹配、方向和稳定排序单测 |
| `tests/contract/test_diff_json_contract.py` | JSON schema、枚举和序列化稳定性 |
| `tests/integration/test_atlas_config_diff.py` | 固定 AtlasConfig 纵向回归 |

Excel 首选 `openpyxl>=3.1,<4` 只读加载 `main`。如果固定样例或真实工作簿因样式问题加载失败，只对 `main` 所需 OOXML 部件使用 `zipfile + ElementTree` 兜底；不解析业务 Sheet、公式、样式或宏。

CSV 使用 Python 标准 `csv` 模块。所有行号与字段规则从 `dataset_layout` 注入，不能在多个模块重复硬编码。

## 8. 分阶段实施

### M2-02：冻结契约

1. 新增 `app/schemas/diff.py`；
2. 确认顶层、Sheet、行、字段、错误与摘要模型；
3. 固定状态枚举和错误码；
4. 实现唯一的规范 JSON 序列化函数；
5. 提供一份小型 `m2.diff.v1` 示例 JSON；
6. 契约评审通过后，后续阶段只能兼容性补字段；破坏性修改必须升级版本。

完成标志：构造模型可输出合法 JSON；同一模型连续序列化结果字节相同。

### M2-03：完成严格解析

1. 实现 Excel `main` 最小解析器；
2. 在 `main` 的有效数据区中精确定位同时包含 `sheetName` 和 `tbxName` 的唯一表头行，建立左右映射；
3. 实现 UTF-8 BOM/UTF-8 CSV 严格解析；
4. 按 CSV 逻辑记录读取第 2/3/4 行和第 8 行后的数据；
5. 校验字段唯一、行宽、主键存在与唯一；
6. 将所有失败转换为固定错误码；
7. 验证原始 Excel 与 CSV 的 SHA-256 前后不变。

完成标志：左右各 16 个逻辑 Sheet 均能映射并解析，或得到可定位的结构化失败。

### M2-04：完成严格 Diff

1. 按 `sheetName` 匹配 Sheet；
2. 按字段名建立列映射；
3. 按 `Id/id` 精确匹配行；
4. 输出 Sheet、行和字段状态；
5. 计算逐层摘要；
6. 按第 6 节规则稳定排序；
7. 通过契约模型校验后输出 JSON。

完成标志：固定 AtlasConfig 结果满足验收真值，并能写出完整 `m2.diff.v1` 文件。

### 纵向收口：本地输出入口

提供单工作簿命令：

```powershell
py -3 -m app.tools.diff_sample `
  --source tests/excel/left `
  --target tests/excel/right `
  --workbook AtlasConfig.xlsm `
  --output .cache/m2/AtlasConfig.diff.json
```

命令只允许读取输入目录；输出只能写到显式 `--output`。不自动遍历其他工作簿，不访问 SVN。

## 9. 测试矩阵

### 9.1 单元测试

- UTF-8 BOM、逗号、引号、嵌入换行；
- 第 2/3/4/8 行定位；
- 字符串前后空格和空字符串；
- 行列重排不产生业务差异；
- source-only、target-only、modified 分类；
- 字段增加、删除和移动；
- `tbxName` 改名但 `sheetName` 相同；
- 空主键、重复主键、重复字段、缺 CSV、坏编码、行宽异常；
- 主键变化不能被行号兜底误配；
- source/target 反转后，方向字段和 source-only/target-only 统计正确翻转。

### 9.2 稳定性测试

- 同一进程连续执行两次，JSON 字节相同；
- 两个独立 Python 进程执行，JSON SHA-256 相同；
- 调换文件系统枚举顺序，JSON 不变；
- 禁止结果出现绝对路径、时间戳、耗时、对象地址和堆栈；
- JSON 经反序列化再通过契约模型序列化，结果不变。

### 9.3 固定样例集成测试

AtlasConfig 验收真值：

| 指标 | 期望 |
|---|---:|
| 逻辑 Sheet | 16 |
| 无差异 Sheet | 7 |
| 有差异 Sheet | 9 |
| source-only 行 | 56 |
| target-only 行 | 39 |
| 修改行 | 273 |
| 修改字段 | 375 |
| 失败 Sheet | 0 |

专项断言：

- `TeamConfig`：15 个 source-only、15 个 target-only；
- `TeamStar`：110 个修改行、24 个 target-only；
- 原始 `.xlsm` 与 CSV 文件哈希在执行前后完全一致。

## 10. 验收项

- [x] `m2.diff.v1` 契约和示例已实现并通过契约测试；
- [x] Pydantic 模型拒绝未知状态和未声明字段；
- [x] 左右 `main` 均映射 16 个 CSV；
- [x] 固定样例统计全部匹配；
- [x] 错误场景返回固定错误码，不静默返回空差异；
- [x] 相同输入跨进程输出 SHA-256 一致；
- [x] 输出中不存在 `old/new` 方向歧义；
- [x] 原始样例保持只读；
- [x] 定向测试和全量测试通过；
- [x] 未修改前端、SVN Provider、端点注册和 M1 快照契约。

## 11. 明确不做

- 不接入 `TableCsv` 的 SVN 读取；
- 不读取 M1 `content_ref`；
- 不新增 Diff HTTP API；
- 不修改 M2-00 页面；
- 不实现“比对全部差异文件”；
- 不做三路 Merge、写回 Excel 或 SVN 写操作；
- 不把 SmartDiff 的行号或模糊配对策略带入正式结果。

## 12. 执行顺序建议

按以下四个可独立验收的变更集推进：

1. **契约变更集**：模型、状态、错误码、规范序列化和契约测试；
2. **解析变更集**：`main` 与 CSV 解析器、派生异常 fixture 和单元测试；
3. **引擎变更集**：严格 Diff、摘要、方向和确定性测试；
4. **纵向变更集**：服务编排、本地命令、AtlasConfig 集成回归与完整 JSON 输出。

任何一个变更集失败时停在本地层修复，不提前用前端或 SVN 掩盖问题。

## 13. 实施结果

- 输出文件：`.cache/m2/AtlasConfig.diff.json`；
- 规范 JSON SHA-256：`430e5ae560fa6c83d6580e40f9e635294149cbdd9cacfb7020df9a2acb59a1e7`；
- 成熟库路径：`openpyxl 3.1.5` 已优先尝试，因固定样例样式表存在重复渐变停靠点失败；
- 兜底路径：只读 OOXML 成功提取左右各 16 条 `main` 映射；
- CSV Diff：16 个 Sheet，7 个 unchanged、9 个 modified，56/39/273/375 真值全部命中；
- 稳定性：两个独立 Python 进程输出 SHA-256 一致；
- 测试：M2 定向 14 passed，全量 135 passed。
