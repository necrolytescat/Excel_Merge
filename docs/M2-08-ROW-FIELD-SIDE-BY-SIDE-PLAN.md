# M2-08 行与字段左右对照表目标与实施计划

> 状态：已验收并归档
> 更新日期：2026-08-09
> 阶段归属：M2-08，仍属于 M2
> 起始提交：`b962a44897769bc55a88d73851b00c0575a5b48e`
> 数据基线：`var/m2-fixtures/d3c-6e501824.m2fixture`

## 1. 目标

将差异结果页中的“行与字段差异”从“主键 + 状态 + 字段按钮”改造成类似
Beyond Compare 的左右工作簿对照表：左侧固定代表 `source=left`，右侧固定代表
`target=right`，同一业务主键实体在左右两侧占据同一视觉行。

本项只重构行与字段的展示、选择和滚动交互，不改变服务端 Diff 结果、工作簿和
Sheet 统计、行配对、主键判定或 source/target 方向。

## 2. 已确认需求

1. 左侧显示左分支表格，右侧显示右分支表格。
2. 左右表格支持鼠标拖拽向上、下、左、右平移。
3. 每个差异实体显示为一条对齐行，身份由当前 Sheet 的真实主键确定。
4. 右侧新增行在左侧显示整行空白占位；右侧删除行在右侧显示整行空白占位。
5. 空白侧不伪造逻辑行号；有数据侧显示契约提供的原始逻辑行号。
6. 左右两侧使用同一字段顺序和列宽，第一列为当前 Sheet 的真实主键，不硬编码
   `Id`。
7. 修改行默认只展示发生修改的字段；当 Sheet 存在新增或删除行时采用方案 A，
   整个 Sheet 展示全部字段。
8. 左右表格的垂直和水平滚动位置同步，不提供两侧独立横向位置。
9. 行顺序保持现有 `sheet.rows` 输出顺序，不按主键或逻辑行号重新排序。
10. 单元格不自动换行；完整值通过悬停提示和选中详情查看。
11. 保留“修改 / 右侧新增 / 右侧删除”的非颜色状态表达。
12. 点击任意一侧单元格时，同时选中左右对应字段，并继续更新已锁定标题中的
    Sheet 和左右逻辑行定位。
13. 只有“修改”状态可点击；每次点击按当前表头顺序定位到本行下一个修改字段，
    到最后一个后回到第一个。新增和删除状态保持不可点击。
14. 修改字段保留现有单元格背景，只在右侧 TARGET 中将相对左侧新增或替换的
    字符标红；纯删除不伪造右侧字符，选中详情继续显示完整纯文本。

## 3. 真实 Replay 基线

夹具信息：

```text
Task: 6e501824-ac7d-49d4-bd7f-6d7136a958f1
Archive SHA-256: 31353872bde2ab53fee8e0d6dfda31196117564361a0d51dfbfc0921487d0380
Inputs: 726
Missing: 2
Golden results: 55
```

本轮只加载黄金结果核对数据形状，没有执行重算或更新黄金结果。

### 3.1 普通修改

`Activity.xlsm / RookieSevenPeriod`：

- 主键：`Id`；
- 3 个修改行、3 个修改字段；
- 可用字段：`Id, PeriodPoint, PeriodRewards, StarNum, StarPic, ItemPic`；
- 没有新增或删除行，因此目标列为 `Id, PeriodRewards`；
- `Id=5`：左侧第 12 行 `210002=180`，右侧第 12 行 `210002=240`。

### 3.2 修改、新增、删除混合

`AtlasConfig.xlsm / TeamStar`：

- 主键：`Id`；
- 36 个修改行、14 个右侧新增行、12 个右侧删除行、82 个修改字段；
- 全部字段：`Id, ForceName, Star, AddAttribute`；
- 因为存在新增和删除行，方案 A 要求整个 Sheet 显示以上全部字段；
- `Id=71`：左侧第 68 行、右侧第 72 行，`Star` 和 `AddAttribute` 修改；
- `Id=61`：左侧为空，右侧第 62 行显示完整目标数据；
- `Id=121`：左侧第 104 行显示完整源数据，右侧为空。

### 3.3 纯新增 Sheet

`TroupeNew.xlsm / HeroVoice`：

- 78 个右侧新增行；
- 每行 8 个字段；
- 左侧显示 78 条对齐空行，右侧显示全部字段和值；
- 已锁定 Sheet 标签仍显示 `+624`，本项不得修改该统计。

### 3.4 长文本

`ShopConfig.xlsm / RefreshShop`：

- `ShopId=510000` 的 `Key_RefreshDesc` 含长文本；
- 表格单元格保持单行，不因长文本改变左右行高；
- 悬停和选中详情必须能读取完整值。

### 3.5 大结果

`Loot.xlsm / Base`：

- 9 个字段；
- 2842 个差异行；
- 6027 个修改字段；
- 用于验证分批或窗口化渲染、拖拽、滚动同步和选择稳定性。

### 3.6 字段本身只存在单侧

`FormationConfig.xlsm / Cell` 的 `Bonusliupai` 为 `target_only` 字段。字段身份仍按
同一列对齐：左右表头使用同一字段名，缺少该字段的一侧显示空单元格并保留字段
缺失状态。不得通过错列或删除表头掩盖字段级差异。

## 4. 目标信息结构

```text
┌ 左侧 SOURCE ─────────────────┬──────────┬ 右侧 TARGET ────────────────┐
│ 行号 │ 主键 │ 字段 A │ 字段 B │  状态    │ 行号 │ 主键 │ 字段 A │ 字段 B │
├──────┼──────┼────────┼────────┼──────────┼──────┼──────┼────────┼────────┤
│  68  │  71  │   5    │ 2=100  │ 修改     │  72  │  71  │  15   │ 2=160  │
│      │      │        │        │ 右侧新增 │  62  │  61  │  35   │ 1=5000 │
│ 104  │ 121  │   5    │ 2=100  │ 右侧删除 │      │      │       │        │
└───────────────────────────────┴──────────┴───────────────────────────────┘
```

约束：

- 左右两侧等宽，中间状态栏保持窄列，不承载字段值；
- 两侧第一列为逻辑行号栏，第二列为冻结主键列；
- 字段表头保持服务端 `fields` 顺序，不按名称排序；
- 表头和主键列在滚动时保持可见；
- 行高固定，左右占位行与实际行严格等高；
- 状态不能只依靠背景色表达。

## 5. 列集合规则

每次选择 Sheet 时只计算一次可见列：

1. 将真实 `primaryKey` 放在第一业务列；
2. 如果存在 `source_only` 或 `target_only` 行，追加该 Sheet 的全部字段；
3. 否则追加所有修改行中 `fields` 的并集；
4. 字段顺序以 `fieldDefinitions` 为准；
5. 不在定义中的合法字段按首次出现顺序追加；
6. 主键若已包含在字段集合中不得重复；
7. 字段定义为 `source_only` 或 `target_only` 时，仍在左右相同列位显示字段名，
   缺失侧单元格为空。

方案 A 的直接结果是：混合 Sheet 的修改行也会显示未变化字段。这些单元格使用
完整的 source/target 行值，但不添加修改高亮。

## 6. 行模型与显示规则

### 6.1 修改行

- 左右显示各自 `row_number` 和完整行值；
- 仅真实变更字段使用修改样式；
- 未变化字段只在方案 A 的全字段 Sheet 中出现；
- 点击任一单元格选中同一字段的左右单元格。

### 6.2 右侧新增行

- 契约状态：`target_only`；
- 左侧行号、主键和全部字段为空；
- 右侧显示目标逻辑行号和完整行值；
- 中间状态显示“右侧新增”。

### 6.3 右侧删除行

- 契约状态：`source_only`；
- 左侧显示源逻辑行号和完整行值；
- 右侧行号、主键和全部字段为空；
- 中间状态显示“右侧删除”。

空白占位与真实空字符串是不同状态。占位行通过行状态和样式表达，不向用户伪造
`—`、`null`、0 或其他业务值。

## 7. 滚动、拖拽与性能

### 7.1 同步滚动

- 左右两侧各自保留原生滚动容器；
- 任一侧滚动时同步另一侧的 `scrollTop` 和 `scrollLeft`；
- 使用同步锁避免两个 `scroll` 事件互相回写形成循环；
- 中间状态栏只同步垂直位置；
- 滚轮、触控板、键盘和滚动条仍可使用。

### 7.2 鼠标拖拽

- 在任一表格按下主鼠标键并拖动即可四向平移；
- 使用 Pointer Events 和 pointer capture；
- 设置移动阈值区分点击选择和拖拽；
- 拖拽期间禁止文本选择，释放或取消指针后恢复；
- 不拦截按钮、链接或其他明确交互控件。

### 7.3 大结果渲染

`Loot/Base` 不允许一次性创建全部复杂行和单元格。首版采用固定行高的窗口化渲染：

- 根据共享 `scrollTop` 计算可见行范围；
- 增加上下缓冲行，避免快速拖动出现空白；
- 左、状态栏、右使用同一窗口索引；
- 使用等高占位保持完整滚动高度；
- 选中状态按行身份和字段名保存，不能依赖临时 DOM 节点；
- 切换工作簿或 Sheet 时销毁旧窗口状态和事件监听。

## 8. 长文本与选中详情

表格单元格：

- 单行显示；
- `text-overflow: ellipsis`；
- 悬停提示包含完整值和字段名；
- 左右空侧提示“该侧无此行”或“该侧无此字段”，不伪造业务值。

选中详情：

- 在 `.diff-main-pane` 内增加紧凑的全宽详情带，不恢复已删除的右侧详情面板；
- 显示业务主键、字段名、左侧完整值、右侧完整值和左右逻辑行号；
- 详情值允许换行和内部滚动，表格行本身仍保持固定高度；
- 选择变化继续调用现有标题定位逻辑，不修改已锁定标题格式。

### 8.1 修改状态字段导航

- “修改”状态使用原生按钮，支持鼠标点击、键盘 Enter 和 Space；
- 当前已选中本行修改字段时，从该字段移动到下一个修改字段；
- 当前选择不属于本行修改字段时，从表头顺序中的第一个修改字段开始；
- 定位后同时选中左右对应单元格，并同步横向滚动使目标字段进入可视区域；
- 单一修改字段的行重复点击时保持在该字段；
- “右侧新增”和“右侧删除”继续使用普通状态文本，不进入键盘焦点顺序。

### 8.2 右侧字符级差异

- 仅处理修改行中的右侧 TARGET 修改字段，左侧 SOURCE 不增加字符标红；
- 使用字符级 LCS 识别多段新增和替换，普通字符保持正常文字色，变化字符使用
  红色半粗文字；
- 修改字段现有黄底继续表达字段级变化，不把整格文字改成红色；
- 右侧纯删除没有可标红字符，只保留字段修改背景，不渲染占位符；
- 使用文本节点和 `span` 组装显示内容，不把工作簿值写入 `innerHTML`；
- 相同前后缀先行裁剪，并限制 LCS 矩阵规模；超过上限时仅将中间目标片段标红，
  保证长文本和窗口化滚动性能；
- 单元格继续单行截断，悬停、无障碍名称和选中详情保留完整目标值。

## 9. 前端 view model 最小扩展

现有 mapper 对修改行只保留 `changes`，没有保留 `row.source.values` 和
`row.target.values`。方案 A 要显示混合 Sheet 修改行中的未变化字段，因此现有
view model 不足以表达已确认需求。

只扩展现有 `m2_diff_mapper.js` 行模型：

```text
sourceRowNumber
targetRowNumber
sourceValues
targetValues
fields              # 保持现有差异字段数组
```

要求：

- `sourceValues/targetValues` 是现有 `m2.diff.v1` 行值的浅拷贝；
- 缺失侧保持 `null`，不转换为带占位符的伪数据；
- 保留现有 `fields`、字段定义、summary 和错误语义；
- 不建立第二套前端 Diff JSON；
- 不修改 `m2.diff.v1`、后端 schema 或 Core Diff。

## 10. 影响文件

计划修改：

- `app/templates/compare_results.html`
  - 仅修改 `.diff-main-pane` 内部的 ready 状态结构；
  - 增加左右表格、状态栏和选中详情带容器。
- `app/static/compare_results.js`
  - 重构 `renderSheet`；
  - 调整 `updateDetail` 以支持配对单元格和详情带；
  - 增加列模型、行模型、窗口化渲染、同步滚动和拖拽辅助函数。
- `app/static/compare_results_readability.css`
  - 增加左右表格、固定表头/主键、占位行、状态、选中详情和响应式样式；
  - 删除或停用只服务旧字段按钮布局的结果页覆盖规则。
- `app/static/m2_diff_mapper.js`
  - 仅增加左右完整行值和行号，不改变契约解释。
- `tests/contract/test_compare_preview.py`
  - 固定新 DOM、资源版本、锁定区域未变化和关键交互契约。
- `tests/contract/test_diff_web_mapping.py`
  - 验证修改行完整左右值、单侧 `null`、真实主键和字段定义顺序。

默认不修改：

- `app/static/compare_results_batch.js`；
- `app/static/compare_results_batch.css`；
- `app/static/offline_replay.js`；
- Batch Task、工作簿导航、比对结果标题区、Sheet 标题与导航；
- `core/**`、`app/schemas/**`、`app/services/**`；
- Replay 夹具和黄金结果。

如果实施中证明必须触碰以上默认冻结文件，应停止并单独说明，不能自行扩大范围。

## 11. 实施步骤

1. 为 mapper 补充失败测试，固定完整左右行值和单侧空值语义。
2. 最小扩展 mapper 行 view model，运行 mapper 契约测试。
3. 在 `.diff-main-pane` 内建立左右表格、状态栏和详情带结构。
4. 实现可见列计算和三类行的成对渲染。
5. 实现单元格选择、双侧高亮、标题定位和完整值详情。
6. 实现双侧滚动同步和鼠标四向拖拽。
7. 实现固定行高窗口化渲染，并用 `Loot/Base` 校验。
8. 完成桌面和窄屏样式，不修改已锁定区域。
9. 运行针对性测试、全量测试和真实 Replay 浏览器验收。
10. 报告实际差异并停止，不继续下一项 M2-08 改造。

## 12. 验收用例

### 12.1 数据与方向

- 左侧始终是 `source=left`，右侧始终是 `target=right`；
- 同一主键只占一条视觉行；
- 修改行左右行号和值正确；
- 右侧新增的左侧为空；
- 右侧删除的右侧为空；
- 不将空侧渲染成 `—`、0 或伪造行号；
- 实际主键名不是 `Id` 时仍正确显示和冻结。

### 12.2 字段

- `Activity/RookieSevenPeriod` 只显示 `Id` 和 `PeriodRewards`；
- `AtlasConfig/TeamStar` 因存在单侧行而显示全部 4 个字段；
- `TroupeNew/HeroVoice` 显示全部 8 个字段和 78 条右侧新增对齐行；
- 字段顺序与 `fieldDefinitions` 一致；
- 字段本身只存在单侧时保持同列对齐，缺失侧为空。

### 12.3 交互

- 在左侧或右侧拖拽均可四向平移；
- 左右纵向和横向位置始终同步；
- 拖拽后没有滚动反馈循环、跳动或行错位；
- 小幅点击不会被误判为拖拽；
- 点击任一单元格同时高亮左右对应字段；
- 点击“修改”状态按表头顺序循环本行修改字段，最后一个之后回到第一个；
- 从非修改字段开始时首次定位到第一个修改字段，单字段修改行重复点击保持不变；
- “修改”状态支持键盘 Enter/Space，新增和删除状态不可点击且不可聚焦；
- `BattlePassConfig/Task` 主键 `11020124` 的 `StartTime` 右侧只标红年份中的 `5`；
- `ItemConfig/Base` 主键 `543` 的 `Describe` 右侧只标红“攻速”中的“速”；
- `Activity/RookieSevenPeriod` 主键 `5` 的 `PeriodRewards` 右侧只标红 `24`；
- 纯删除不产生伪字符，新增行、删除行和未修改字段不执行字符级标红；
- 已锁定标题继续显示正确的左右逻辑行定位；
- `ShopConfig/RefreshShop` 长文本不撑高表格行，悬停和详情均可查看完整值。

### 12.4 性能与响应式

- `Loot/Base` 2842 行切换、拖拽和连续滚动无明显长时间阻塞；
- 快速滚动时左右和状态栏不出现不同窗口或空白断层；
- 选择状态经过虚拟窗口回收后仍能恢复；
- 1440x900、1280x800、390x844 下无重叠；
- 窄屏允许表格区域内部滚动，不挤压或重排锁定的上级导航。

### 12.5 状态与回归

- modified、target_only、source_only 均有文字状态；
- partial 保留成功 Sheet 和结构化错误；
- failed、未执行和无差异状态不误显示表格；
- 正式、Replay、Demo 继续共享同一结果页渲染；
- 正式页不依赖 Demo 数据；
- Batch Task、工作簿导航、标题区和 Sheet 导航的 DOM、统计与交互不变化。

## 13. 测试命令

```powershell
node --check app/static/m2_diff_mapper.js
node --check app/static/compare_results.js
py -3 -m pytest -q tests/contract/test_diff_web_mapping.py
py -3 -m pytest -q tests/contract/test_compare_preview.py
py -3 -m pytest -q
git diff --check
```

本项目自动化基线：

```text
121 passed, 1350 warnings
```

`D:\Excel_Merge` 直接执行无路径限定的 `pytest` 还会收集被主仓库 Git 忽略的
`reference/smartdiff` 外部参考仓库 91 项测试，因此显示 212 项；这 91 项不属于当前
工作树的项目测试基线。

浏览器验收只使用既有 Replay 夹具、`tests/excel/left`、`tests/excel/right`、契约示例
和自动化测试数据，不访问 SVN、不运行新正式任务、不更新黄金结果。

## 14. 风险与控制

| 风险 | 控制方式 |
|---|---|
| 双向 scroll 事件形成反馈循环 | 单一同步锁和统一滚动状态 |
| 左右行高不一致造成错位 | 固定行高、单元格不换行、共享窗口索引 |
| 拖拽吞掉点击或文本详情 | 移动阈值、pointer capture、明确取消路径 |
| 大结果 DOM 过多 | 固定行高窗口化渲染和缓冲区 |
| 虚拟行回收后丢失选择 | 选择状态按主键、行状态和字段名持久化 |
| 方案 A 缺少未变化字段值 | 最小扩展现有 mapper，保留完整左右 values |
| 字段级单侧差异错列 | 使用统一字段身份和 definitions 顺序 |
| 误改锁定区域 | 限制 DOM/CSS/JS 修改范围并保留契约断言 |
| 方向语义被文案混淆 | 代码保持 source/target，UI 固定左侧/右侧和右侧新增/删除 |

## 15. 不可变约束

- 保持 `m2.diff.v1` 和 `m2.batch.v1`；
- 保持 `source=left`、`target=right`；
- Excel 与 CSV 来自同侧冻结 Revision；
- SVN 只读，不运行新的正式任务；
- 不执行 Merge、Excel 写回或 SVN 写操作；
- 不修改 Core 解析、配对、主键或 Diff 语义；
- 不新增第二套前端 Diff JSON；
- 正式结果页不依赖 Demo 假数据；
- Replay 不更新黄金结果；
- 不访问允许范围外的业务数据。

## 16. 实施门禁

本文件完成不代表功能改造已获授权。只有用户明确确认本目标和计划后，才从第 11 节
第 1 步开始实施；实施范围仅限本项，不顺带修改其他 M2-08 区域。

## 16.1 字段显示名双行表头授权补充

本节记录用户在原实施完成后新增并明确授权的范围，优先于第 9、10、13、15 节中
关于 Core、后端 schema、Replay 黄金结果冻结的旧描述。

目标与规则：

- 每个业务字段表头固定显示两行：第一行为 CSV 第 1 条逻辑记录中的
  `display_name`，第二行为 CSV 第 2 条逻辑记录中的稳定 `field_name`；
- 两行使用独立文本元素，不依赖自动换行；主键列使用同一字段定义，行号列保持
  单行“行号”；
- 左侧优先使用 `source_display_name`，右侧优先使用
  `target_display_name`；本侧中文名为空或本侧不存在字段时，回退到另一侧中文名；
- 两侧第二行始终显示统一的 `field.name`。字段身份、字段顺序和对齐仍只以
  `field.name` 为准；
- `display_name` 只作为展示元数据，不参与字段状态、字段匹配、主键判定、行配对
  或值差异计算；左右中文名不同本身不能令字段状态变为 `modified`；
- 表头采用新的固定高度，并同步更新窗口化渲染的 `DIFF_HEADER_HEIGHT`，避免首行
  偏移、遮挡或滚动窗口计算错误。

最小契约扩展：

- `CsvField` 增加 `display_name`；解析器保留本来已经读取的第 1 行值；
- 现有 `m2.diff.v1` 的字段定义增加可选 `source_display_name` 和
  `target_display_name`，mapper 原样保留；不新增第二套前端 Diff JSON；
- 允许修改 `core/table_csv_parser.py`、`app/schemas/diff.py`、
  `app/services/workbook_diff_service.py` 及相关测试，但不得改变 Core 解析规则、
  字段匹配、配对、主键或 Diff 语义；
- 允许只使用 `var/m2-fixtures/d3c-6e501824.m2fixture` 内冻结输入离线重算并更新
  该夹具现有黄金结果。不得访问 SVN、运行正式任务、更换输入或扩大数据范围。

补充验收：

- `Activity/RookieSevenPeriod` 的主键和差异字段分别显示“流水ID / Id”、
  “阶段奖励 / PeriodRewards”；
- 左右显示名不同、某侧显示名为空、source-only 和 target-only 字段均符合侧别优先与
  回退规则；
- 显示名变化不改变字段、Sheet 或工作簿 Diff 状态；
- `Loot/Base` 的窗口化数量、滚动同步和选择行为不因表头增高而回归。

## 17. 实施结果

本项已按用户确认执行，实际改动限于第 10 节列出的结果页模板、JS、CSS、mapper
和相关契约测试。`m2.diff.v1`、`m2.batch.v1`、Core Diff、Batch Task、工作簿导航、
标题区、Sheet 标题与导航均未改动。

验收结果：

- 项目测试：`121 passed, 1350 warnings`；
- JavaScript 语法检查和 `git diff --check` 通过；
- `Activity/RookieSevenPeriod` 显示 `Id + PeriodRewards`，`Id=5` 左右均为第 12 行；
- `AtlasConfig/TeamStar` 显示全部 4 个业务字段，新增/删除空侧和真实行号正确；
- 鼠标四向拖拽后左右 `scrollTop/scrollLeft` 与状态栏垂直位置同步；
- `TroupeNew/HeroVoice` 显示 78 条右侧新增和全部 8 个业务字段；
- `ShopConfig/RefreshShop` 长文本保持单行截断，悬停和选中详情保留完整值；
- `Loot/Base` 的 2842 行使用窗口化渲染，验收时每侧只创建 18 条可见行；
- 1440x900 与 390x844 截图检查无重叠，窄屏使用模块内部横向滚动。

实施后缺陷修复：

- 指针捕获延后到移动超过 5px 后执行，普通点击和小幅移动点击均能命中单元格；
- 普通选中格不再提升到冻结列之上，横向滚动后行号和主键仍保持正确命中；
- 真实 Replay 验证点击后左右对应字段同时选中，拖拽后左右与状态栏继续同步。
- “修改”状态按表头顺序循环定位修改字段；`AtlasConfig/TeamStar` 的 `Id=71`
  在 `Star` 与 `AddAttribute` 之间循环，Enter 与鼠标点击行为一致；
- 从非修改字段触发时回到首个修改字段；`Activity/RookieSevenPeriod` 的单字段行
  重复触发时保持 `PeriodRewards`；新增/删除状态保持不可点击；
- 状态导航继续复用窗口化行渲染，`Loot/Base` 的 2842 行验收时仅渲染 18 个
  可见状态按钮。
- 右侧 TARGET 修改字段已增加字符级标红；`BattlePassConfig/Task/11020124/StartTime`
  仅标红 `5`，`ItemConfig/Base/543/Describe` 仅标红“速”，
  `Activity/RookieSevenPeriod/5/PeriodRewards` 仅标红 `24`；
- `AtlasConfig/TeamStar/77/Star` 从 `35` 变为 `5` 时没有伪造红色字符，黄底、
  悬停完整值和无障碍名称保持不变；
- 字符级分段只创建于右侧可见修改单元格；`Loot/Base` 2842 行验收时仍只渲染
  18 行，页面无脚本异常。


## 18. 字段显示名双行表头实施结果

本项已按第 16.1 节授权实施：

- `CsvField.display_name` 保留 CSV 第 1 条逻辑记录；
- 现有 `m2.diff.v1` 字段定义增加可选 `source_display_name` 和
  `target_display_name`，字段身份仍为 `field.name`；
- 左右表头固定显示“中文显示名 / 字段名”两行，主键列使用相同规则，行号列保持
  单行；本侧显示名为空或字段不存在时回退另一侧；
- 表头和中间状态表头固定为 52px，窗口化计算同步使用 52px；
- 静态资源版本更新到 `2.1.2`；
- 使用夹具内冻结输入离线重算并更新 55 个黄金结果，归档 SHA-256 从
  `31353872bde2ab53fee8e0d6dfda31196117564361a0d51dfbfc0921487d0380`
  更新为 `bde0ff57c39cf53c9370ab76b5c496f9d2129b06b9336695204b8662c501e296`。

验证结果：

- display_name 全链路针对性测试：37 passed；
- 排除 5 个依赖工作树缺失 `config/settings.json` 的既有模块后：87 passed；
- 完整测试在收集阶段因该配置缺失失败，未复制主仓库配置或生成替代文件；
- JavaScript 语法检查和 `git diff --check` 通过；
- `Activity/RookieSevenPeriod` 左右表头显示“流水ID / Id”和
  “阶段奖励 / PeriodRewards”，表头实测 52px；
- `FormationConfig/Cell/Bonusliupai` 为真实 target-only 字段，左侧正确回退右侧
  中文名“布阵格提供流派层数”，并保持缺失侧状态；
- `Loot/Base` 滚动到 1400px 后左右和状态栏各只渲染 24 行，左右
  `scrollTop/scrollLeft` 完全一致；
- 夹具没有左右显示名不同或 source-only 字段样本，因此这两类使用已授权自动化测试
  数据覆盖，没有伪造 Replay 业务数据。


## 19. 字段视图切换目标与计划

### 19.1 已确认目标

在“重算当前工作簿 / 重新比对当前工作簿”旁增加两段式字段视图切换：

- `显示差异`：默认模式，保留现有方案 A。纯修改 Sheet 只显示主键和修改字段；
  只要 Sheet 存在新增或删除行，继续显示全部字段；
- `显示原表`：行集合仍为现有差异行，不加入完全无变化行；列集合展开为当前 Sheet
  的全部字段；
- 两种模式都保留左右方向、字段顺序、双行表头、冻结主键、行状态、字段高亮、
  TARGET 字符标红、空侧占位和窗口化渲染；
- 模式在当前页面内跨工作簿和 Sheet 保持，刷新后恢复默认“显示差异”。

真实 Replay 基线：

- `Activity/RookieSevenPeriod`：6 个字段、3 条修改行；差异模式显示
  `Id + PeriodRewards`，原表模式显示全部 6 个字段，行数仍为 3；
- `AtlasConfig/TeamStar`：4 个字段、36 条修改、12 条删除、14 条新增；因方案 A，
  两种模式都显示全部 4 个字段；
- `Loot/Base`：9 个字段、2842 条差异行且含 7 条新增；因方案 A，两种模式都显示
  全部 9 个字段。

### 19.2 最小实现

- 仅解锁 `.workbench-heading-actions`，加入字段视图分段控件；不修改标题、说明、
  统计、Batch Task、工作簿导航或 Sheet 标题与导航；
- 在结果页 state 增加页面级字段视图模式；
- 列模型在原表模式使用全部 `fieldDefinitions`，差异模式继续使用既有规则；
- 切换时重建当前虚拟表格，保留当前 Sheet、纵向滚动位置和可继续表达的选中字段；
- 共享正式、Replay 和 Demo 结果页实现，不新增第二套前端 Diff JSON；
- 不修改 Core、后端 schema、mapper、Replay 夹具或黄金结果。

计划影响文件：

- `app/templates/compare_results.html`；
- `app/static/compare_results.js`；
- `app/static/compare_results_readability.css`；
- `tests/contract/test_compare_preview.py`；
- `tests/contract/test_diff_web_mapping.py`。

### 19.3 风险与验收

- 大字段表横向宽度增加：继续使用表格内部横向滚动，不改变行窗口化数量；
- 切换导致审阅位置丢失：保留纵向滚动和同一行业务选择，横向位置按新宽度钳制；
- 原表模式误加入无变化行：断言行数始终等于 `sheet.rows`；
- 方案 A 被错误覆盖：新增/删除 Sheet 在差异模式仍显示全部字段；
- 窄屏操作区重叠：1440x900、1280x800、390x844 检查标题操作区和内部滚动；
- `Activity/RookieSevenPeriod` 在两种模式间应为 2 列与 6 列切换，3 条差异行
  不变；
- `Loot/Base` 切换前后字段数均为 9，滚动同步和可见窗口数量不回归。

### 19.4 实施结果

本项已按第 19 节授权完成，实际改动限于结果页模板、前端列模型与状态、样式及相关
契约测试；未修改 Core、后端 schema、mapper、Replay 夹具或黄金结果。静态资源版本
更新到 `2.1.3`。

实现结果：

- 在 `.workbench-heading-actions` 内增加“显示差异 / 显示原表”两段式控件；
  页面刷新默认“显示差异”，页面内跨工作簿和 Sheet 保持当前模式；
- “显示差异”继续执行方案 A，“显示原表”只展开当前 Sheet 全部字段，不增加无变化行；
- 模式切换保留纵向和可用横向滚动位置；按字段名重算选中列。原表模式选中的未修改
  字段切回差异模式时，回退到同一行首个修改字段；
- 两种模式继续复用双行表头、左右同步、空侧占位、状态导航、TARGET 字符标红和
  行窗口化；390px 窄屏下新操作控件改为上下两行。

验证结果：

- 字段视图相关契约测试：`11 passed`；
- 排除 5 个依赖工作树缺失 `config/settings.json` 的既有模块后：`88 passed`；
- JavaScript 语法检查和 `git diff --check` 通过；
- 真实 Replay `Activity/RookieSevenPeriod`：差异模式为“行号 + Id +
  PeriodRewards”3 列，原表模式为“行号 + 全部 6 字段”7 列，均为 3 条差异行；
- 原表模式选中 `PeriodPoint` 后切回差异模式，同一主键 `Id=5` 回退到
  `PeriodRewards`；
- 真实 Replay `AtlasConfig/TeamStar`：模式跨工作簿保持；因存在新增/删除行，
  两种模式均显示全部 4 个业务字段；
- 真实 Replay `Loot/Base`：切换前后均为 9 个业务字段，`scrollTop=1400`、
  `scrollLeft=420` 时左右和状态栏位置完全一致，每侧只渲染 25 行；
- 1440x900、1280x800 和 390x844 下新操作控件无重叠且文字完整；390px 下 Replay、
  Batch 等既有锁定区域仍有历史横向溢出，本项未越界修改。

## 20. 归档结论

2026-08-09 用户完成验收，本计划转为只读归档依据。后续若继续调整结果页，必须作为
新的确认项重新评估，不得在本归档范围内顺带修改。正式归档记录见
`docs/archive/M2-08-ROW-FIELD-DIFF-ARCHIVE.md`。
