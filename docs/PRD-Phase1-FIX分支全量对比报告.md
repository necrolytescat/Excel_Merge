# PRD · Phase 1：FIX 分支跨版本全量对比与按人归因报告

> 适用性声明（2026-08-09）：本文档是早期调研方案，仅保留作 SmartDiff、语义 Diff 和报告设计参考。其中 CSV/TBX、同 URL 双 Revision、日期输入、逐提交回放及 CLI 主流程均不是当前实现契约，不得覆盖已交付的版本对比模块。当前有效定义以 `VERSION-COMPARISON-HANDBOOK.md`、`ROADMAP.md`、`contracts/` 和相关 ADR 为准。

| 项 | 值 |
|---|---|
| 文档版本 | v1.1 |
| 日期 | 2026-08-04 |
| 阶段 | Phase 1（共 N 期，后续为 Merge / 跨区域对比 / LLM 摘要） |
| 上游依据 | `docs/调研报告.md` |
| 面向读者 | AI 开发（本文档需可直接驱动实现，不留模糊语义） |
| 状态 | 待评审 |

> **v1.1 变更（相对 v1.0）**：按用户反馈移除两项——(1) **不再解析/对比 `main` 表**（历史导出配置表，且 CSV↔XLSM 归集改用文件名约定，不依赖 `tbxName`）；(2) **删除第 7 行「字段设定人」及由此衍生的「越界修改」分析**（属过度设计）。相应删除 F6.3、ChangeItem 的 `column_owner`/`cross_owner`、验收 A6、场景「责任确认」、`author_alias.json` 的越界用途，并将 `isExport=0` 的 sheet 盲区一并移除（其检测依赖 `main`）。

---

## 0. 一句话定义

> 选定 **同一个 FIX 分支** 的两个 SVN 版本，全量对比区间内所有配置表的语义差异，输出一份 **HTML 修改报告**，回答两个问题：**（A）这个版本相比上个版本变成了什么样？（B）这段时间每个人分别改了什么？**

---

## 1. 范围界定

### 1.1 Phase 1 做什么

| 编号 | 能力 | 说明 |
|---|---|---|
| S1 | 单分支双版本全量对比 | 同一 SVN URL，`rev_from` → `rev_to` |
| S2 | 逐提交回放归因 | 区间内每个 revision 独立算 diff，精确归因到人 |
| S3 | 区间净差异 | 两端点直接对比，回答"最终变成什么样" |
| S4 | 语义级 diff | 主键匹配行、代码名对齐列，非文本行 diff |
| S5 | 按工作簿归集 | 报告以 XLSM 工作簿为组织单元（由 CSV 文件名约定推导），而非零散 CSV |
| S6 | HTML 单文件报告 | 无外部依赖，双击可开，可发群、可归档 |

### 1.2 Phase 1 明确不做

| 不做 | 理由 / 归属阶段 |
|---|---|
| Merge / 三路合并 / 写回 XLSM | Phase 2+ |
| 跨区域对比（KR vs JP） | Phase 3；需列增删识别，Phase 1 同分支列结构稳定 |
| Dev vs Fix 跨分支对比 | Phase 2；需 `svn mergeinfo` |
| LLM 摘要 | Phase 4；先保证确定性 diff 可信 |
| 多人 Web 服务 / 账号体系 | 本地运行即可 |
| SVN 写操作 | 全程只读，永不 commit |
| `main` 表解析与对比 | 历史导出配置表，Phase 1 不依赖；归集改用文件名约定 |
| 第 7 行「字段设定人」与越界分析 | 死 metadata，无分析价值，属过度设计 |

### 1.3 关键决策记录（已拍板）

| 决策项 | 结论 | 影响 |
|---|---|---|
| 归因口径 | **净差异 + 逐提交明细 双层** | 逐提交回放为主链路，净差异额外算一次 |
| diff 数据源 | **CSV 为准，按工作簿归集** | 归集关系由 CSV 文件名约定推导，**不再解析 `main` 表** |
| 报告形态 | **HTML 单文件** | 内联 CSS/JS，无 CDN 依赖 |
| 规模假设 | **按中等规模设计**（数百提交 × 数百表） | 必须做内容缓存 + 异步任务 + 进度反馈 |

---

## 2. 术语表

| 术语 | 定义 |
|---|---|
| **XLSM** | 策划编辑的源工作簿，含宏，单源真相（Phase 1 仅作可选元数据源，不作 diff 输入） |
| **CSV / TBX** | 由 XLSM 导出的版本化文本产物，diff 的实际输入；文件名约定为 `{工作簿}_{sheet}.csv` |
| **`main` 表** | 历史导出配置表（列 `sheetName\|tbxName\|isExport\|creator\|desc`）。**Phase 1 不解析、不对比**，仅作背景说明 |
| **代码名** | 数据表第 2 行的英文字段标识（如 `Id` / `SeasonNum`），**列匹配的唯一依据** |
| **表头块** | 数据表第 1–6 行，含显示名/代码名/类型/导出端/分隔/备注（第 7 行责任人已废弃，忽略） |
| **净差异** | `rev_from` 与 `rev_to` 两端点直接对比的结果 |
| **逐提交明细** | 区间内每个 revision 相对其前一个状态的 diff |
| **哑变更** | 值前后相等的伪差异（空白、编码、行尾、数字格式导致） |

---

## 3. 事实基础（已实测确认）

> 以下为 `reference/table/ArenaPeak.xlsm` 实测结论，实现时按此编码。凡与本节冲突的旧文档描述以本节为准。

### 3.1 数据表结构（全库统一）

```
行 1  中文显示名     IndexID | 备注      | 赛季期数   | Key_赛季名称
行 2  代码名 ★       Id      | Remarks   | SeasonNum | Key_Name
行 3  类型           uint32  | transtring| uint32    | string
行 4  导出端 ★       All     | None      | Client    | Client
行 5  空行分隔       -       | -         |           | -
行 6  中文备注       流水ID  | -         | 第n赛季填n| -
行 7  字段设定人(已废弃，Phase 1 忽略)
行 8+ 数据           1001    | 第一赛季  | 1         | Ar_Ba_Name_1001
```

- **主键 = A 列**，代码名恒为 `Id` / `id`（行 1 显示名不固定：IndexID / indexID / ID / 流水ID）。
- **导出端 = `None` 的列不进 CSV**（如上例 `Remarks`）。
- **第 7 行字段设定人不再使用**：Phase 1 不读取、不对比、不据此做任何分析。

### 3.2 `main` 表不再使用（背景说明）

`main` 表是历史导出配置表，结构为 `sheetName | tbxName | isExport | creator | desc`，原用途是声明每个 sheet 是否导出 CSV 及其导出文件名（`tbxName`）。

**Phase 1 不解析 `main` 表、不对比它。** 原因：

- 它是旧导出管线的配置残留，对「比较两个版本的差异」无信息价值；
- CSV↔XLSM 的归集改用**文件名约定**（见 §3.3），不依赖 `tbxName`。

### 3.3 CSV↔XLSM 归集约定（替代 main）

SVN 上 CSV 文件名约定为 `{工作簿名}_{sheet名}.csv`（如 `ArenaPeak_Banner.csv`）。据此：

- **工作簿** = 文件名中第一个 `_` 之前的部分（`ArenaPeak`）；
- **sheet** = 第一个 `_` 之后的部分（`Banner`）；
- 对应 XLSM 即 `{工作簿名}.xlsm`（同目录）。

> ⚠️ 若工作簿名自身含 `_`（如 `My_Game_Base.csv`），约定会产生歧义；此时降级为「整段文件名即一个工作簿」。详见待确认 U3。

### 3.4 已知解析陷阱（仅当读取 XLSM 元数据时触发）

| 陷阱 | 处置 |
|---|---|
| **openpyxl 无法打开真实表** | 实测抛 `ValueError: Duplicate position 0.0`（样式表 gradientFill 重复停靠点）。**禁用 openpyxl**，一律走 `zipfile` + `xml.etree` 直读 |
| sharedStrings 体量大 | 单文件实测 17009 条。按需索引，不要全量转 dict 后再遍历 |
| sheet 名 → 物理文件非顺序对应 | 必须走 `xl/workbook.xml` + `xl/_rels/workbook.xml.rels` 的 rId 映射，**禁止**假设 `sheet1.xml` 就是第一个 sheet |

### 3.5 CSV 为准策略的唯一已知盲区

必须在报告中**显式声明**，不可静默忽略：

- **导出端 = `None` 的列**（中文备注、策划注释等）的改动不会被检出。

> 报告页脚固定输出："本报告基于导出 CSV 生成，CSV 未包含的列（导出端=None，如中文备注/作者列）其改动不会被检出。" 若能定位到对应 XLSM 并读取其表头块（行 1–6），则进一步给出未覆盖列数 N；否则仅作定性声明。

---

## 4. 用户与使用场景

**主要用户**：游戏项目策划、主策、QA、版本负责人（Windows 环境，已装 TortoiseSVN）

| 场景 | 描述 | 命中能力 |
|---|---|---|
| 版本提测前自查 | 「这次 FIX 送测相比上次，配置改了哪些」 | S3 净差异 |
| 事故回溯 | 「上周三到今天，谁动过战斗数值表」 | S2 按人明细 |
| 交接与周报 | 「这个迭代每个人配置产出是什么」 | S2 + S5 |

---

## 5. 功能需求

### F1 · 对比任务配置

**输入项**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `repo_url` | string | 是 | FIX 分支 SVN URL |
| `rev_from` | int \| `HEAD` \| ISO 日期 | 是 | 区间起点（不含，即 diff 从 `rev_from+1` 起算） |
| `rev_to` | int \| `HEAD` \| ISO 日期 | 是 | 区间终点（含） |
| `path_filter` | string[] | 否 | 子路径白名单，默认全仓库 |
| `author_filter` | string[] | 否 | 只看指定提交人 |
| `include_net_diff` | bool | 否 | 是否算净差异，默认 true |
| `svn_username` / `svn_password` | string | 否 | 留空则用本机凭据缓存 |

**约束**
- `rev_from < rev_to`，否则报错拒绝执行。
- 日期形式转换为 `{YYYY-MM-DD}` 交给 svn 解析，不自行换算。
- 密码**禁止**落盘明文；仅内存持有，日志中一律脱敏为 `***`。

### F2 · 提交清单拉取

单次调用取全量元信息：

```bash
svn log -v --xml -r {rev_from+1}:{rev_to} {repo_url}
```

**产出** `CommitInfo[]`：

```jsonc
{
  "revision": 1123,
  "author": "wuchangjiang",
  "date": "2026-07-21T10:32:11.000000Z",
  "message": "调整赛季奖励",
  "changed_paths": [
    { "path": "/trunk/fix/table/ArenaPeak_Base.csv", "action": "M", "copyfrom_path": null, "copyfrom_rev": null }
  ]
}
```

**要求**
- `action` 取值 `A`(新增) / `M`(修改) / `D`(删除) / `R`(替换)。
- `copyfrom_path` 非空即为**重命名或复制**，报告中需单列，不可当作「删一个 + 加一个」。
- 只保留后缀命中 `.csv` / `.tbx`（可配置）且落在 `path_filter` 内的路径。
- `svn log` 输出可能是 GBK 或 UTF-8，须做**编码回退解码**（先 UTF-8，失败退 GBK），参考 `svn_helper.py:_decode_output`。
- Windows 下所有 svn 子进程须带 `CREATE_NO_WINDOW`，避免弹黑窗。

### F3 · 内容获取与缓存

**取内容命令**（必须用 peg revision 形式，否则文件被重命名后取不到）：

```bash
svn cat {repo_url}/{path}@{revision}
```

**缓存设计（Phase 1 性能核心）**

SVN 中 `(path, revision)` 的内容**永久不可变**，因此可无限期缓存：

```
cache_key = sha1(f"{repo_uuid}|{path}|{revision}")
cache_path = .cache/content/{key[:2]}/{key}.bin
```

**拉取次数最优解**

朴素实现为每个 revision 的每个文件取 old + new，共 `2 × Σ变更条目` 次。正确实现应为：

> 对每个文件 `f`，需要的版本集合 = `{rev_from}` ∪ `{f 在区间内被修改的所有 revision}`
> 总拉取次数 = `文件数 + 总变更条目数`（约为朴素实现的一半，且缓存命中后趋近于 0）

原因：`r_i` 的 new 内容即 `r_{i+1}` 的 old 内容，滑动窗口复用即可。

**并发**：线程池并发拉取，默认 `max_workers=6`，可配置。须提供全局限流以防打爆 SVN 服务器。

**失败处理**：单文件拉取失败不中断任务，记入 `errors[]`，报告中单列「未能获取」区块。

### F4 · 解析

#### F4.1 CSV 解析器（diff 主输入）

**产出统一字典**（与 smartdiff `xml_differ` 完全同构，以便直接复用引擎）：

```jsonc
{
  "sheets": {
    "ArenaPeak_Banner": {
      "headers": ["Id", "Key_Name", "Name", "Type", "BannerResource"],
      "rows": [
        { "_row": 1, "_key": "1", "cells": { "A": "1", "B": "Ar_Ba_Name_1", "C": "启程冲刺", "D": "1", "E": "UI/Sprite/..." } }
      ]
    }
  }
}
```

**表头来源优先级**
1. CSV 自带表头行 → 直接使用；
2. CSV 无表头 → 按列位置生成 `Col_A` / `Col_B` ...（若能通过 §3.3 约定定位 XLSM 并读取其表头块行 2，则用代码名替换）。

**编码与规范化**
- 编码探测顺序：UTF-8-BOM → UTF-8 → GBK；均失败则记错误并跳过该文件。
- 行尾统一为 `\n`；末尾空行剔除。
- 分隔符可配置，默认 `,`；须正确处理引号包裹与转义。

**哑变更抑制**（默认开启，可关闭）
- 单元格值两端空白裁剪后比较；
- 数值型列（行 3 类型为 `uint32/int32/float` 等）按数值比较，`1.0` 与 `1` 不算差异；
- 纯格式差异（如 `1,000` vs `1000`）视配置决定。

#### F4.2 XLSM 元数据采集（可选 best-effort 增强）

**目的**：为报告提供字段显示名、并检测「导出端=None」列以填充盲区声明。本模块**不读取 `main` 表、不读取第 7 行**。

**输入**：按 §3.3 文件名约定定位到的 XLSM（取 `rev_to` 版本；定位失败则整体跳过）。
**实现**：`zipfile` + `xml.etree.ElementTree` 直读，**禁用 openpyxl**。

**产出** `SheetMeta`（仅数据表头块行 1–6）：

```jsonc
{
  "workbook": "ArenaPeak.xlsm",
  "sheets": [{
    "sheet_name": "Banner",
    "columns": [
      { "index": 0, "code": "Id",             "display": "IndexID",      "type": "uint32", "export": "All",    "note": "流水ID" },
      { "index": 4, "code": "BannerResource", "display": "banner资源路径","type": "string", "export": "Client", "note": "-" }
    ]
  }]
}
```

**降级策略**：XLSM 未找到 / 解析失败 / 与 CSV 列数不匹配时，**不强制对齐**，报告以 CSV 原生信息（代码名或 `Col_X`）呈现，并在页脚标注「未能定位 XLSM 元数据，显示名与盲区统计可能不完整」。

### F5 · 语义 Diff

**复用**：`reference/smartdiff/xml_differ.py`（292 行，格式无关，实测 11/11 通过）

**必须改造的点**

| 问题 | smartdiff 现状 | Phase 1 要求 |
|---|---|---|
| 列对齐 | 按**列字母**对齐（`xml_differ.py:229,252`），插一列即雪崩（实测 1 列插入 → 12 个假差异） | 改为按**代码名**对齐。Phase 1 同分支列结构基本稳定，但仍必须改，否则策划加一列就全表假差异 |
| 主键识别 | 自动探测 | 固定为 A 列，`Id`/`id` 校验 |

**行匹配**：沿用三轮策略（主键 → 内容哈希 → 行号），实测可抗插入/删除/重复 ID。

**输出变更类型**（枚举，实现须完整覆盖）

| 类型 | 说明 |
|---|---|
| `cell_modified` | 单元格值变更，带 old/new |
| `row_added` / `row_deleted` | 按主键判定的行增删 |
| `column_added` / `column_removed` | 按代码名判定的列增删 |
| `file_added` / `file_deleted` | CSV 文件级增删 |
| `file_renamed` | 由 `copyfrom_path` 识别 |
| `duplicate_key` | 同一 CSV 内主键重复，**告警不中断** |

### F6 · 归因与聚合

#### F6.1 逐提交明细（主链路）

对区间内每个 revision `r`：
1. 取该 revision 变更的 CSV 列表；
2. 每个文件对比 `content(f, prev_rev_of_f)` vs `content(f, r)`；
3. 该 revision 产出的所有 `ChangeItem` 全部归因于 `commit.author`。

> 此处 `prev_rev_of_f` 是**该文件自己的上一个版本**，不是 `r-1`。实现时对每个文件维护独立游标。

#### F6.2 净差异

`content(f, rev_from)` vs `content(f, rev_to)`，逐文件对比一次。仅用于回答「最终变成什么样」，**不参与归因**。

#### F6.3 聚合维度

统一变更记录 `ChangeItem`：

```jsonc
{
  "revision": 1123,
  "author": "wuchangjiang",
  "date": "2026-07-21T10:32:11Z",
  "message": "调整赛季奖励",
  "workbook": "ArenaPeak.xlsm",
  "sheet": "Banner",
  "csv_file": "ArenaPeak_Banner.csv",
  "change_type": "cell_modified",
  "row_key": "1",
  "column_code": "Name",
  "column_display": "玩法名",
  "old_value": "启程冲刺",
  "new_value": "启程之路"
}
```

聚合视图（报告直接消费）：
- **按人**：`author → workbook → sheet → 变更列表`
- **按表**：`workbook → sheet → 行 → 变更列表（含多人先后修改）`
- **按时间**：`revision 倒序时间线`

### F7 · HTML 报告

**技术要求**
- **单文件**，CSS/JS 全内联，无 CDN、无外链，离线可用。
- 数据以 `<script type="application/json">` 内联，前端渲染。
- 数据量大时（> 5000 条变更）启用虚拟滚动或分页，避免卡死。
- 中文字体栈须含 `Microsoft YaHei`。

**信息架构**

```
┌ 报告头
│   分支 URL · 版本区间 · 生成时间 · 覆盖声明（盲区提示）
│   统计卡：提交数 / 参与人数 / 涉及工作簿数 / 变更单元格数
├ Tab 1 · 按人（默认）
│   每人一张卡片：头部（姓名/提交数/变更数）
│   └ 展开 → 按工作簿分组 → 按 sheet 分组 → 变更明细表
│       明细列：revision | 行主键 | 字段(显示名+代码名) | 旧值 → 新值
├ Tab 2 · 按表
│   工作簿 → sheet → 行主键 → 该行所有变更（含多人先后修改的演进链）
├ Tab 3 · 净差异
│   区间两端点对比结果；对被中途改回的项标注「区间内曾变更但最终无净差异」
├ Tab 4 · 时间线
│   revision 倒序，每条含 author / message / 影响表数 / 变更数
└ 报告尾
    覆盖声明 · 错误清单（拉取失败/解析失败/主键重复）· 生成参数快照
```

**交互要求**
- 全局搜索：按人名 / 表名 / 字段名 / 值内容过滤。
- 快捷筛选：「只看行增删」「只看某人」。
- 旧值→新值并排展示，差异字符高亮（可参考 smartdiff 的单元格 LCS 字符级高亮）。
- 长文本值折叠，点击展开。

**配色**：数值增大用红、减小用绿（中国区习惯）；新增用蓝、删除用灰。

---

## 6. 技术架构

### 6.1 模块划分

```
core/
  svn_client.py      F2/F3  svn log/cat 封装、编码回退、CREATE_NO_WINDOW、凭据脱敏
  content_cache.py   F3     (path,rev) 内容缓存，sha1 分桶落盘
  csv_parser.py      F4.1   编码探测、统一字典输出、哑变更规范化
  xlsm_meta.py       F4.2   可选：zipfile+ET 直读数据表头块(行1-6)，不读main/行7
  differ.py          F5     移植 smartdiff xml_differ + 代码名列对齐改造
  attributor.py      F6     逐提交回放、净差异、聚合
  report_html.py     F7     单文件 HTML 生成
cli.py                      命令行入口
config/
  settings.json             并发数、后缀白名单、哑变更开关
```

### 6.2 复用与自研边界

| 来源 | 内容 | 处置 |
|---|---|---|
| smartdiff `xml_differ.py` | 三轮行匹配算法 | **移植后改造**列对齐逻辑 |
| smartdiff `svn_helper.py` | `_decode_output` 编码回退、`_run_raw` 二进制通道、URL 百分号解码、`CREATE_NO_WINDOW` | **抄工程实践**，不抄 API 形态 |
| smartdiff `server.py` | — | **不复用**，单工作区全局状态与需求架构冲突 |
| smartdiff `xlsx_parser.py` | — | **不复用**，依赖 openpyxl，真实表打不开 |
| 全新自研 | `csv_parser` / `xlsm_meta` / `attributor` / `report_html` | Phase 1 主要工作量 |

### 6.3 执行流程

```
1. 校验参数、探活 SVN、取 repo_uuid
2. svn log -v --xml           → CommitInfo[]（1 次网络调用）
3. 过滤路径、构建「文件 → 需拉取版本集合」
4. 线程池拉取内容（缓存优先）  → 进度回调
5. （可选）按 §3.3 约定定位 rev_to 的 XLSM → SheetMeta（失败则跳过）
6. 逐 revision 回放 diff       → ChangeItem[]（进度回调）
7. 端点净差异 diff             → ChangeItem[]
8. 三维聚合
9. 渲染 HTML → 输出路径
```

### 6.4 任务与进度

中等规模下总耗时以分钟计，须异步执行并上报进度：

```jsonc
{ "stage": "fetching", "current": 128, "total": 460, "message": "拉取 ArenaPeak_Base.csv@r1123" }
```

阶段枚举：`init` → `logging` → `fetching` → `parsing` → `diffing` → `aggregating` → `rendering` → `done` / `failed`

---

## 7. 异常与边界

| 场景 | 处置 |
|---|---|
| SVN 不可达 / 认证失败 | 立即失败，明确提示是网络还是凭据问题 |
| 区间内无任何 CSV 变更 | 正常出报告，显示「本区间无配置表变更」 |
| 文件在区间内被删除 | 记为 `file_deleted`，不再拉后续版本 |
| 文件被重命名 | 由 `copyfrom_path` 串联新旧路径，作为同一文件的连续历史 |
| CSV 主键重复 | 告警不中断，报告错误区列出 |
| 区间内发生列增删（结构变更） | diff 照常产出 `column_added/removed`，报告标注「区间内发生过表结构变更」；不强制依赖 XLSM 对齐 |
| 单文件解析失败 | 跳过并记录，不影响整体 |
| 空提交（无文件变更） | 时间线保留，变更数为 0 |
| author 为空 | 归入 `(unknown)` 分组 |
| 超大 CSV（> 50MB） | 流式解析，禁止全量 `read()` 后再 split |
| 路径含中文 / URL 编码 | 统一百分号解码后再比较 |
| XLSM 元数据未定位 | 报告降级为 CSV 原生信息，页脚标注元数据不完整 |

---

## 8. 验收标准

### 8.1 功能验收

| 编号 | 验收项 | 通过标准 |
|---|---|---|
| A1 | 区间对比可跑通 | 指定 FIX URL 与两版本，产出 HTML 报告 |
| A2 | 归因正确 | 抽查 5 条变更，revision/author 与 TortoiseSVN 日志一致 |
| A3 | 覆盖修改-撤回 | 构造 A 改值、B 改回场景：明细 Tab 显示两条，净差异 Tab 显示无变更 |
| A4 | 主键匹配抗行序 | 中间插入 10 行后重排，diff 只报新增 10 行，无假变更 |
| A5 | 列对齐抗插列 | 新增 1 列，diff 只报 `column_added`，**其后列不产生假差异**（对照缺陷 4.5.2 的 12 个假差异） |
| A6 | 归集正确 | CSV 均归到正确工作簿下，映射来自文件名约定（§3.3），不依赖 main 表 |
| A7 | 盲区声明 | 报告尾部正确声明「导出端=None 列」未被覆盖；能定位 XLSM 时给出未覆盖列数 |
| A8 | 单文件可移植 | HTML 拷到无网机器双击正常渲染与交互 |
| A9 | 错误不中断 | 人为让 1 个文件拉取失败，其余正常出报告且错误区列出该文件 |

### 8.2 性能验收（中等规模基线）

| 指标 | 目标 |
|---|---|
| 200 提交 × 300 表 首次运行 | ≤ 10 分钟 |
| 同参数二次运行（缓存命中） | ≤ 1 分钟 |
| 网络拉取次数 | ≤ `文件数 + 总变更条目数`（禁止 2N 朴素实现） |
| HTML 报告体积 | ≤ 20 MB（超出则自动降级为摘要 + 附 JSON） |
| 报告首屏渲染 | ≤ 3 秒 |

### 8.3 质量要求

- 引擎层（differ / csv_parser / attributor）单元测试覆盖率 ≥ 70%。
- 必须包含 A3 / A4 / A5 三个场景的自动化回归用例。
- 全程只读 SVN；代码中出现任何 `svn commit` / `svn ci` 视为验收不通过。

---

## 9. 待确认项

> 以下项**不阻塞开发启动**，各自给出默认假设；确认后如与假设不符，按标注的影响面调整。

| 编号 | 问题 | 默认假设 | 影响面 |
|---|---|---|---|
| U1 | 导出 CSV 是否自带表头行？ | 假设**不带**，从 XLSM 元数据推导列名（CSV 有表头则直接用） | `csv_parser` 表头逻辑，已设计双路兼容 |
| U2 | CSV 编码与分隔符？ | UTF-8-BOM / UTF-8 / GBK 探测，分隔符 `,` | 探测失败率；已有回退 |
| U3 | CSV 文件名 → 工作簿归集约定是否可靠？ | `X_Sheet.csv` → 工作簿 `X`（首个 `_` 前）；若工作簿名含 `_` 则降级为整段即工作簿 | S5 归集准确性；若不可靠则改为按 CSV 路径直接组织报告 |
| U4 | 后缀白名单是否含 `.tbx`？ | 含 `.csv` 与 `.tbx` | 配置项，改配置即可 |
| U5 | SVN 上 CSV 与 XLSM 的目录布局？ | 同仓库同目录或邻近目录，文件名约定可定位 XLSM | `xlsm_meta` 能否增强显示名与盲区统计 |
| U6 | 区间典型跨度（提交数/表数）？ | 中等规模（数百 × 数百） | 若实际为千级，需加持久化缓存与断点续跑 |
| U7 | 是否需要命令行以外的 GUI？ | Phase 1 仅 CLI，报告即产物 | 若需 GUI 则 Phase 1.5 追加 |
| U8 | 非导出列（导出端=None）改动是否需覆盖？ | Phase 1 不覆盖，仅声明 | 若必须覆盖则需并行 XLSM diff 链路（工作量显著上升） |

---

## 10. 后续阶段衔接

| 阶段 | 内容 | 依赖 Phase 1 的产出 |
|---|---|---|
| Phase 2 | Dev vs Fix 跨分支对比 + `svn mergeinfo` | 复用 svn_client / differ / report |
| Phase 3 | 跨区域对比（KR vs JP） | 复用全部；需强化列增删识别 |
| Phase 4 | Merge 与值级写回 XLSM | 复用 `xlsm_meta` 的表头块（行 1–6）列定位能力 |
| Phase 5 | LLM 摘要与风险提示 | 直接消费 `ChangeItem[]` 结构化输出 |

> Phase 1 的 `ChangeItem` 是全链路的核心数据契约，后续阶段均以其为输入。**该结构一旦定稿不应随意变更**。

---

## 附录 A · smartdiff 关键代码位置索引

| 内容 | 位置 | Phase 1 用途 |
|---|---|---|
| 三轮行匹配 | `xml_differ.py:150-292` | 移植 |
| ID 列自动检测 | `xml_differ.py:17-51` | 参考，改为固定 A 列 |
| 列按字母对齐（缺陷根因） | `xml_differ.py:229,252` | **必须改造** |
| 编码回退解码 | `svn_helper.py:_decode_output` | 抄 |
| 二进制输出通道 | `svn_helper.py:_run_raw` | 抄 |
| 单元格字符级高亮 | `static/` 前端 | 报告交互参考 |
| 单 target + 双 revision | `svn_helper.py:323-393` | 反面教材，Phase 1 即为此场景但需重写 |

## 附录 B · 实测验证脚本

| 脚本 | 验证内容 | 结论 |
|---|---|---|
| `docs/verify/v1_duplicate_id.py` | 重复 ID 是否漏检 | 未漏检 |
| `docs/verify/v2_celltype_fidelity.py` | 合并写回类型保真 | 有缺陷（Phase 1 不涉及写回） |
| `docs/verify/v3_column_shift.py` | 插入列 diff 噪音 | 严重缺陷，A5 验收项即针对此 |

