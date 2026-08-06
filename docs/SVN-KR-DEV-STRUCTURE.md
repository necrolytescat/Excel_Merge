# KR/DEV SVN 目录结构基线

> 本文保留 KR/DEV 的历史拓扑和数量实测。当前 M1/M2 读取职责与配对规则以 ADR-007 和 `dataset_layout` 配置为准。

## 读取范围

- SVN 地址：已配置的 Resource/Trunk_KR 端点（主机地址不写入文档）
- 分支：KR / DEV
- 检查 Revision：HEAD（读取时仓库 Revision 为 26400）
- 最近变更 Revision：26399
- 本文只记录 Source/table 和 Source/TableCsv，不代表其他目录可以参与当前 Diff。

## 实际目录

SVN 路径区分大小写，当前实际目录为：

- Source/table：源 Excel 文件目录
- Source/TableCsv：导出的 CSV 文件目录

口述中的 tabel 应按实际路径 Source/table 处理，TableCsv 的 T 和 C 为大写。

## 文件统计

在当前 HEAD 下：

- Source/table：200 个文件
  - 197 个 .xlsm
  - 2 个 .xlam
  - 1 个 .txt
- Source/TableCsv：874 个 .csv

因此首版不能只假设源文件扩展名为 .xlsx，应至少支持 .xlsm；.xlam 和 .txt 暂按非主配置源文件处理。

## Excel 与 CSV 的关系

一个 Excel 可以导出多个 CSV，但正式归属关系不再通过文件名主体猜测：

- 从候选 Excel 的 `main` Sheet 读取导出清单；
- `sheetName` 定义逻辑 Sheet 和最终展示名称；
- `tbxName` 精确定位 `TableCsv/{tbxName}.csv`；
- 文件名主体匹配只能用于诊断，不能作为正式 Diff 归属规则；
- CSV 缺失必须报告失败，不能当成无差异。

```text
Excel 工作簿
└─ main 清单行
   ├─ sheetName：逻辑 Sheet
   └─ tbxName：对应 CSV 文件
```

## 对后续实现的约束

1. 端点目录配置应使用精确大小写路径。
2. M1 文件级筛选只读取 `Source/table` Excel；M2 只对候选工作簿读取对应的 `Source/TableCsv` 文件。
3. 源文件类型首版至少包含 .xlsm；是否处理 .xlam 需产品确认。
4. CSV 归属必须使用 `main.sheetName` 与 `main.tbxName`，不得依赖文件名主体推断。
5. 选择端点后，`Table` 与 `TableCsv` 自动绑定到该端点的同一冻结 Revision，不能分别选择。
6. 双端点 Diff 先筛 Excel 候选，再建立候选工作簿到 CSV 的显式映射。
7. 目录读取和文件读取均使用冻结 Revision，不能依赖本地工作区状态或再次读取 HEAD。

本文是 KR/DEV 的实测基线。JP、TC、BT 以及 FIX 分支接入后，需要分别重新读取并确认目录是否一致。
## 历史可读取范围

针对当前 KR/DEV：

- SVN 分支根的 info 探测可以返回 Revision 1 的元信息，但这不代表业务目录在 Revision 1 已存在；
- Source/table 和 Source/TableCsv 在 Revision 22804 读取不到；
- 两个目录从 Revision 22805 开始可读取；
- Revision 22805 的提交时间为 2026-05-07 19:56:15（北京时间）；
- Revision 22805 是 Trunk_KR 从 Trunk_Tc@22804 创建的分支复制提交；
- Revision 22805 时已有 192 个源文件和 844 个 CSV 文件。

因此，当前 KR/DEV 两个业务目录的有效历史下界应按 Revision 22805 处理，而不是按仓库全局 Revision 1 处理。
