# M2 离线数据矫正夹具运行手册

> 格式：`m2.fixture.v1`  
> 页面：`/compare/replay`（仅 `web.dev_mode=true`）  
> 安全边界：只读原始字节、纯内存会话、临时目录计算；不访问 SVN、不写批量 SQLite、不执行宏或公式、不接 Merge/写回。

## 1. 真实 D3-C 夹具

| 项目 | 值 |
|---|---|
| 文件 | `var/m2-fixtures/d3c-6e501824.m2fixture` |
| Task | `6e501824-ac7d-49d4-bd7f-6d7136a958f1` |
| Source | `KR_FIX_KR-Fix-1.0.0.0` @ r26438 |
| Target | `KR_FIX_KR-Fix-1.0.1.0` @ r26438 |
| 归档 SHA-256 | `bde0ff57c39cf53c9370ab76b5c496f9d2129b06b9336695204b8662c501e296` |
| 归档大小 | `46,210,882 bytes` |
| 原始输入 | 726 个 Excel/CSV 索引项，按 SHA-256 内容寻址去重 |
| 显式缺失 | 2 个 |
| 黄金结果 | 55 个 `m2.diff.v1` |
| 任务摘要 | 54 succeeded，1 business_failed |

显式缺失项是 `MainActivity.xlsm` 的双侧
`MainActivity_FunctionName.csv`。SVN r26438 实际存在大小写变体文件
`MainActivity_FunctIonName.csv`；原导出器只做精确文件名匹配，未将其字节写入
夹具，因此留下两条显式缺失记录。这是旧夹具输入缺口，不是源数据缺陷。

当前解析器已支持 TableCsv 直接子文件的唯一 `casefold` 完全匹配，但 Replay
不能从归档外恢复缺失字节，所以当前 `1 business_failed` 仅反映该夹具缺口，
不代表修复后的 SVN 配对流程仍然失败。

2026-08-06 经评审显式更新黄金结果：缺少唯一 `Id/id` 时允许使用物理第一列
业务字段作为主键。`HeroConfig/Reborn_TransferLevel`、
`PetConfig/ValueQuality` 和 `ShopConfig/RefreshShop` 已纳入新基线；旧基线保存在
`var/m2-fixtures/d3c-6e501824.pre-first-column-key.m2fixture`，SHA-256 为
`f62564f37f9101c116cf910224f1234bc2869b5df9d269d707e7684e8f509fc0`。

2026-08-09 经用户授权，为双行字段表头保留 CSV 第 1 条逻辑记录的 `display_name`，
并从夹具内冻结输入重算 55 个黄金结果。输入字节、Task、Revision、候选范围和
`54 succeeded / 1 business_failed` 摘要未变化；当前归档哈希和大小以上表为准。

## 2. 为什么同时保存 Excel、CSV 和 Diff

- Excel 保存 `main` 清单、公式缓存和 Excel Table 范围，是 CSV 配对依据；
- CSV 保存字段名、类型、scope、业务行和原始行号，是语义 Diff 的真实输入；
- 黄金 Diff 保存本轮已评审规则下的固定输出，用于发现代码回归；
- SHA-256 只证明字节未变，不能单独证明业务判断正确；正确性仍依赖已评审规则、代表样本和保留的源数据错误。

因此，只加载黄金 JSON 只能调试结果页；只有从原始 Excel/CSV 离线重算并与黄金结果比较，才能验证当前 Diff 数据是否发生变化。

## 3. Web 使用

1. 启动服务：`py -3 -m app.main`；
2. 打开 `http://127.0.0.1:5566/compare/replay`；
3. 选择 `.m2fixture` 后点击“加载”；
4. “黄金结果”用于回放本轮固定结果；
5. 点击“重算全部”后切到“当前重算”，页面会显示一致/不一致计数；
6. 工作簿列表中的“重算当前工作簿”只计算所选项。

页面刷新时，如果服务进程未重启，会恢复内存中已加载的夹具；服务重启后需要重新选择文件。夹具不会写入服务数据库。

## 4. 命令行导出与校验

导出已完成任务：

```powershell
py -3 -m app.tools.export_offline_fixture 6e501824-ac7d-49d4-bd7f-6d7136a958f1
```

导出器通过 SQLite `mode=ro` 读取任务和结果，通过现有
`SVNWorkbookDatasetResolver` 按任务内固定 Revision 只读获取原始数据。输出为确定性 ZIP：相同任务、原始输入和黄金结果会生成相同字节与 SHA-256。

## 5. 包结构

```text
manifest.json
inputs.json
missing-files.json
blobs/<sha256>
expected/task.json
expected/results/<item-id>.json
audit/task-items.json
```

- `manifest.json` 声明所有成员的 SHA-256、大小和用途；
- `inputs.json` 把 side/workbook/filename 映射到内容寻址 blob；
- `missing-files.json` 显式记录清单要求但不存在的 CSV；
- `expected/task.json` 保留严格 `m2.batch.v1` 任务形状；
- `expected/results` 保留严格 `m2.diff.v1` 黄金结果；
- `audit/task-items.json` 保留每项状态、尝试次数、时间和结果哈希。

## 6. 加载门禁

加载器拒绝以下内容：

- ZIP 路径穿越、绝对路径、反斜杠路径、重复或大小写重复成员；
- 加密成员、链接、未知压缩方式、超大归档或超大解压结果；
- 未声明成员、未引用 blob、成员哈希或大小不一致；
- `m2.batch.v1` / `m2.diff.v1` 契约不合法；
- 任务、结果、工作簿身份不一致；
- 任一 `isExport=1` CSV 既没有原始 blob，也没有显式缺失记录。

归档不保存 SVN URL、凭据、绝对路径或可执行内容。正式模式不注册 replay 页面与 API，访问返回 404。

## 7. 当前验证结果

真实夹具使用当前代码完全离线重算 55 项：

```text
current=55, matched=55, mismatched=0
```

这表示当前实现与本轮黄金输出逐字节一致。后续数据矫正规则造成预期变化时，先审查不一致项，再显式生成新黄金夹具；不得在加载时自动覆盖黄金结果。
