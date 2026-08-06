# M1 开发交接说明：版本对比与 Table Excel 快照

> 状态：已归档  
> 归档日期：2026-08-05  
> 归档验收：真实 KR FIX 对比通过；两端各 197 个 Excel，筛选出 53 个文件级差异候选；全量测试 114 passed。

## 1. 最终产品规则

M1 的页面名称为“版本对比”。用户在左右输入框输入分支目录名，从系统配置匹配到的主干/FIX 候选或已登记端点中确认两个不同端点：

```text
左端点：FIX1.1.0
右端点：fix1.0.0（线上分支）
```

点击“锁定并读取快照”时，系统分别读取两个端点的当前 `HEAD`，立即解析成具体 Revision，并以该 Revision 生成两份独立、只读、可复现的快照。用户不输入日期，也不手工选择 Revision；Revision 只存在于快照元数据和文件清单中。

M1 的读取范围固定为每个端点逻辑目录 `Table` 下递归的全部 Excel 文件：`.xlsx`、`.xlsm`、`.xls`。逻辑目录的真实 SVN 路径由 M1-01 发现并绑定，兼容大小写和上层目录差异。其他目录、CSV、TBX、Excel 工作簿解析、Sheet/单元格 Diff、报告和 SVN 写操作均不属于 M1；M1 允许基于快照路径与内容哈希筛选文件级差异候选。

## 2. 当前实现

- `app/templates/compare.html`：版本对比大宽度正式页；无日期入口、无 Revision 下拉框，快照状态内显示阶段进度与耗时。
- `app/static/compare.js`：分支目录名匹配、候选登记、快照调用、逻辑路径归一化和文件级差异候选筛选。
- `app/schemas/svn.py`：端点注册表、快照请求和响应模型。
- `app/services/snapshot_service.py`：端点校验、HEAD 冻结、Table 路径发现、Excel 过滤、并发只读和失败清单。
- `core/svn_provider.py`：Mock/CLI Provider 的二进制 `read_bytes()` 能力；CLI 仍只调用 `svn info`、`svn list`、`svn cat`。
- `app/api/svn.py`：端点注册、目录发现和快照接口。
- `app/services/config_service.py`：端点注册表原子持久化。

当前契约测试覆盖页面范围、端点注册表、大小写路径、HEAD 冻结、跨分支缓存隔离、Excel 过滤、物理路径持久化、文件级候选和单文件失败；全量测试 114 passed。

## 3. M1-01：正式 SVN 拓扑确认

M1-01 的业务确认对象是每个真实 FIX 端点的：

1. 端点 URL 是否可达；
2. `Table` 逻辑目录对应的实际相对路径；
3. 实际路径的大小写和上层层级；
4. 端点是否已启用、标签和区域/轨道归属。

服务通过递归树发现目录 basename（大小写不敏感）为 `Table` 的候选，并将选定的实际相对路径写入注册表。页面始终显示逻辑名 `Table`，不把物理路径写成业务规则。

分支候选同样以系统配置为准：`trunk_branch` 按名称大小写不敏感匹配，`fix_pattern` 中的 `x` 表示数字版本段。未匹配规则的 feature、dev、test 等目录不进入 FIX 候选列表。

## 4. M1-02：端点注册表

注册表记录多个具体 FIX 端点，结构如下：

```json
{
  "id": "KR_FIX_1_1_0",
  "region": "KR",
  "track": "FIX",
  "label": "FIX1.1.0",
  "url": "https://svn.example/repo/branches/KR-fix-1.1.0",
  "logical_scopes": ["TABLE"],
  "physical_path_filters": {"TABLE": "Resource/Table"},
  "enabled": true
}
```

`logical_scopes` 固定规范化为 `TABLE`；`physical_path_filters` 保存实际 SVN 相对路径。注册表不保存日期或用户指定的 Revision。

## 5. 快照接口与流程

用户侧请求只携带两个端点 ID：

```json
{
  "source": {"endpoint_id": "KR_FIX_1_1_0"},
  "target": {"endpoint_id": "KR_FIX_1_0_0"}
}
```

`POST /api/svn/snapshots` 的应用层流程：

1. 校验两个端点存在且启用；
2. 解析真实 URL 和 `Table` 物理路径；
3. 对每个端点只解析一次当前 HEAD，得到冻结 Revision；
4. 在冻结 Revision 上递归列出绑定目录；
5. 只保留 `.xlsx`、`.xlsm`、`.xls` 文件；
6. 批量读取二进制内容和文件元信息；
7. 返回两份独立快照、冻结 Revision、统计和失败清单；
8. 基于逻辑相对路径和内容哈希筛选文件级差异候选；不执行工作簿、Sheet、单元格级 Diff，也不执行任何 SVN 写操作。

文件内容通过内部缓存引用返回，不把完整二进制直接放入 JSON。缓存键必须包含仓库身份、端点 URL、规范化路径和冻结 Revision。

## 6. 预览页契约

- 左侧/右侧使用分支目录名输入匹配，展示具体端点名称和脱敏 URL；
- 端目录固定展示 `Table`，并标注“全量 Excel”；
- 确认按钮文案为“锁定并读取快照”；注册表为空、端点未选全或两端相同时禁用，选择合法端点后可执行；
- 快照摘要明确“确认时冻结 HEAD”，展示解析后的 Revision；
- 03 清单只显示仅左、仅右、内容哈希不同或读取失败的文件级候选，保留相对路径、最后修改人、修改时间、版本号；
- 页面保持大宽度布局；
- 页面不展示日期、手工 Revision、左右快照并列字段或工作簿/Sheet/单元格级 Diff 结果。

## 7. 验收标准

- 可以选择两个具体 FIX 端点并提交快照请求；
- 每个端点的 HEAD 在确认时冻结，后续读取统一使用该 Revision；
- 只返回 `Table` 下的 Excel，递归路径和大小写差异均可处理；
- 其他目录和非 Excel 文件被排除；
- 文件元信息包含最后修改人、修改时间和版本号；
- 单文件读取失败进入失败清单，端点不可达时任务整体失败；
- 快照可按冻结 Revision 复现；
- 真实 KR FIX 验证：两端各 197 个 Excel，文件级差异候选 53 个，物理路径均为 `Source/table`；
- M1 不生成工作簿或单元格级 Diff 结果，不产生 SVN 写操作。

## 8. 延迟执行假设

如果 21:00 锁定后分支继续提交，而任务在更晚时间才开始，当前 HEAD 已经变化，不能代表 21:00 状态。M1 通过确认时立即冻结 Revision 解决正常流程；要支持延迟执行，需要后续保存专用快照或固定端点，不通过日期选择补救。

## 9. 归档边界

M1 归档后只接受阻断性缺陷修复，不再扩展功能范围。后续工作从 M2 开始，负责读取候选工作簿的 `main` 清单，并对配对 CSV 执行业务语义 Diff。

## 10. M2 后续配对规则

每个已选端点在 M2 中同时代表同一冻结 Revision 下的一组只读数据：

- `Table`：Excel 源文件；M1 仍只用它生成文件级候选；
- `TableCsv`：Excel 的可靠 CSV 导出；M2 按候选工作簿 `main.tbxName` 定位 CSV；
- 两类目录不能分别选择端点或 Revision；
- M2 必须复用 M1 已冻结 Revision，不得再次解析 HEAD。

该规则记录在 `dataset_layout` 配置和 ADR-007 中。它是 M2 的消费契约，不改变 M1 当前服务只扫描 `TABLE` Excel 的实现。

## 11. 非目标

- Excel 工作簿、Sheet、单元格解析；
- CSV/TBX 读取和语义 Diff；
- Diff 报告、Merge、Commit、Update、Copy；
- 本地工作区同步和多用户权限。
