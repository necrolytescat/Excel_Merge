# SVN 端点模型与注册表

> M1 端点模型已冻结并归档（2026-08-05）。

## 1. 端点定义

端点是一个可被用户选择的、稳定的具体 SVN 路径，而不是“区域 + 模糊分支规则”。一个区域可以同时登记多个 FIX 版本端点。

```text
id                  稳定唯一 ID，例如 KR_FIX_1_1_0
region              区域代码，例如 KR
track               轨道，例如 FIX
label               页面展示名称，例如 FIX1.1.0
url                 具体 SVN URL
logical_scopes      固定为 ["TABLE"]
physical_path_filters 逻辑目录到实际相对路径的绑定
enabled             是否允许用户选择
```

示例：

```json
{
  "id": "KR_FIX_1_0_0",
  "region": "KR",
  "track": "FIX",
  "label": "fix1.0.0",
  "url": "https://svn.example/repo/branches/KR-fix-1.0.0",
  "logical_scopes": ["TABLE"],
  "physical_path_filters": {"TABLE": "Resource/table"},
  "enabled": true
}
```

端点配置不保存日期、HEAD 或用户选择的 Revision。Revision 是一次快照任务在确认时解析出的不可变元数据。

## 2. M1 逻辑目录与物理路径

业务规则只使用逻辑目录名 `Table`（内部规范化为 `TABLE`）。M1-01 通过 SVN 目录树发现 basename 大小写不敏感的 `Table` 目录，并将实际相对路径保存到 `physical_path_filters`。因此 `Resource/Table`、`resource/table` 和带上层目录的路径都可以绑定，页面不提前硬编码物理路径。

M1 仅读取绑定目录下递归的 `.xlsx`、`.xlsm`、`.xls`。CSV、TBX、其他目录和 SVN 写操作被排除。

分支候选列表不直接把 `branches` 下所有目录都当作 FIX：系统先按区域配置的 `trunk_branch` 和 `fix_pattern` 进行大小写不敏感匹配，仅匹配结果可进入端点登记流程。

### 2.1 M2 配对数据目录

端点选择同时确定一个 M2 数据集：

```text
同一端点 + 同一冻结 Revision
├─ Table     Excel 源文件，用于筛选工作簿候选和读取 main 清单
└─ TableCsv  可靠导出的 CSV，用于业务值 Diff
```

用户不能为 `TableCsv` 另选端点或 Revision。左右端点分别冻结后，各自的 `Table` 和 `TableCsv` 自动绑定到该端点的同一 Revision。

该规则由根级 `dataset_layout` 配置描述，不改变 M1 端点注册表仍只包含 `logical_scopes=["TABLE"]` 的契约，也不使 M1 快照服务扫描 CSV。M2 接入阶段才按 `main.tbxName` 读取 `TableCsv/{tbxName}.csv`。

## 3. 注册表校验

- ID 只允许字母、数字、点、下划线和连字符；
- ID 必须唯一；
- URL 不得携带账号或密码；
- URL 协议必须在允许列表内；
- `logical_scopes` 只能是 `TABLE`（输入大小写可兼容，保存时统一）；
- 物理路径必须是安全的相对路径，不允许 `..`；
- 只有 `enabled=true` 的端点能参与快照；
- 保存采用原子替换，避免半写入配置。

## 4. 版本对比请求

页面只提交两个 ID：

```json
{
  "source": {"endpoint_id": "KR_FIX_1_1_0"},
  "target": {"endpoint_id": "KR_FIX_1_0_0"}
}
```

服务分别执行一次 `svn info -r HEAD`（或等价只读 Provider 调用），将 HEAD 转为具体 Revision；后续 `svn list` 和 `svn cat` 全部使用该 Revision。用户不能通过 M1 页面手工替换 Revision，也不能输入日期。

## 5. 与后续 Diff 的关系

M1 的输出是两份独立快照和文件 manifest，并可基于逻辑 Table 相对路径与内容哈希筛选文件级差异候选。M1 不解析工作簿、Sheet 或单元格，不输出语义 Diff 报告。

M2 对候选 Excel 读取 `main` 清单，以 `sheetName` 作为逻辑 Sheet、以 `tbxName` 定位同端点同 Revision 的 CSV，并按 `Id/id` 计算行与字段差异。最终按“工作簿 → Sheet → 行 → 字段”展示。完整决策见 `docs/adr/ADR-007-m2-table-tablecsv-pairing.md`。
