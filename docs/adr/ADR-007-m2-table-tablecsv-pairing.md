# ADR-007：M2 的 Table 与 TableCsv 配对规则

> 状态：已接受  
> 日期：2026-08-05

## 背景

SVN 端点中的 `Table` 保存 Excel 源文件，`TableCsv` 保存由这些工作簿可靠导出的 CSV。M1 已经冻结为只扫描 `Table` Excel、按路径与内容哈希筛选文件级候选；M2 需要在不改变 M1 契约的前提下，对候选工作簿执行稳定的业务内容 Diff。

工作簿的 `main` Sheet 是导出清单，不是业务数据。清单中的 `sheetName` 表示最终展示的逻辑 Sheet，`tbxName` 定位 `TableCsv/{tbxName}.csv`。

## 决策

1. 一个端点数据集由同一端点、同一冻结 Revision 下的 `Table` 和 `TableCsv` 组成。
2. 用户只选择左右端点，不单独选择 CSV 端点或 Revision；冻结端点后，两类目录自动绑定。
3. M1 继续只读取 `Table` 下的 Excel，用于文件级候选筛选，不扫描 CSV。
4. M2 仅对 M1 选出的 Excel 候选读取 `main` 清单，再按 `tbxName` 到同端点的 `TableCsv` 读取 CSV。
5. `sheetName` 是跨版本匹配和展示名称；`tbxName` 只负责定位 CSV。若 `tbxName` 改名但 `sheetName` 相同，仍按同一逻辑 Sheet 比较。
6. CSV 第 2 行为字段名、第 3 行为类型、第 4 行为范围、第 8 行起为数据；行主键按 `Id`、`id` 顺序识别。
7. `main`、`配置公式2`、公式、格式和宏不参与业务内容 Diff。
8. CSV 缺失、重名、结构不合法或解析失败必须进入失败结果，不能视为无差异。
9. 比较方向固定为 `source=left`、`target=right`；当前不推断 `old/new`。
10. 最终结果按 Excel 逻辑结构展示：工作簿 → `sheetName` → 行 `Id` → 字段。

## 系统配置

`config/settings.json` 和示例配置中的 `dataset_layout` 是该规则的机器可读契约，包含：

- `binding_policy.same_endpoint=true`；
- `binding_policy.same_frozen_revision=true`；
- `workbook_source.directory_name=Table`；
- `csv_export.directory_name=TableCsv`；
- `main` 清单字段、CSV 命名模板、元数据行和主键候选。

## 影响

- Excel 用于确定候选与恢复业务组织结构，CSV 用于实际值比较。
- 避免 Excel 公式缓存、样式和宏产生业务无关噪声。
- M2 接入 SVN 时必须复用 M1 冻结结果，不能再次读取 HEAD。
- 本 ADR 不授权 SVN 写操作，也不要求本阶段实现批量文件 Diff。
