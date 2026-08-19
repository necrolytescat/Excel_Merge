# Excel Diff/Merge 平台

面向游戏项目策划的本地 Web 工具，用于从两个 SVN 端点冻结 Revision，并对 Table Excel 与同侧 TableCsv 生成版本差异报告。

平台只读取 SVN 和工作簿数据，不执行 SVN `commit`、`merge`、`update`，也不会修改或写回 Excel 文件。

## 主要能力

- 版本对比：冻结左右 SVN 端点版本，生成文件、工作表、行和字段级差异；
- 表格计划对比：保存可复用的表格范围，并对多个目标分支运行矩阵对比；
- 版本监控：通过独立 Runner 和 Windows 计划任务定时生成增量报告；
- 历史任务：恢复任务、查看运行日志，并管理可再生的 SVN 缓存；
- 离线 Replay：不访问 SVN，使用仓库内夹具验证差异结果页面。

## 环境要求

必需环境：

- Windows 10/11；
- Python 3.10 或更高版本；
- Git，以及访问本仓库的权限。

可选环境：

- SVN CLI：连接真实 SVN 时需要，命令 `svn` 必须可用；
- Node.js：仅用于前端 JavaScript 静态检查，不影响 Web 服务运行。

在 PowerShell 中检查必需环境：

```powershell
git --version
py -3 --version
```

如需连接真实 SVN，再检查：

```powershell
svn --version --quiet
```

## 安装

### 方式一：让 Codex/Agent 自动安装

`AGENTS.md` 是 Agent 的项目规则文件，本身不会执行安装。将下面这一句话发送给 Codex 或其他能操作本机终端的编码 Agent：

> 请阅读本项目的 `README.md` 和 `AGENTS.md`，在 Windows PowerShell 中检查环境并完成安装：创建 `.venv`、安装 `requirements.txt`、以 Mock 模式启动服务并验证 `/api/health`；如果缺少 Python、Git、SVN 等系统级依赖或需要额外权限，先说明影响并征得我的确认。

这条指令默认只完成安全的本地 Mock 安装和健康检查，不会访问真实 SVN，也不会修改仓库业务数据。

### 方式二：手动安装

如果还没有下载仓库，在 PowerShell 中执行：

```powershell
git clone https://github.com/necrolytescat/Excel_Merge.git
Set-Location Excel_Merge
```

如果已经位于项目根目录，可直接创建独立的 Python 虚拟环境并安装依赖：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

安装完成后启动服务：

```powershell
.\.venv\Scripts\python.exe -m app.main
```

首次启动会自动从 `config/settings.m0.example.json` 生成本机配置 `config/settings.json`。该文件已被 Git 忽略，默认使用 Mock Provider，因此首次体验不需要 SVN。

## 启动与验证

服务启动后打开：

- 首页：<http://127.0.0.1:5566>；
- 健康检查：<http://127.0.0.1:5566/api/health>；
- 版本对比：<http://127.0.0.1:5566/compare>。

也可以在另一个 PowerShell 窗口验证健康状态：

```powershell
Invoke-RestMethod http://127.0.0.1:5566/api/health
```

看到正常响应即表示安装成功。使用 `Ctrl+C` 停止服务；以后在项目根目录重新执行以下命令即可启动：

```powershell
.\.venv\Scripts\python.exe -m app.main
```

## 连接真实 SVN

1. 确认 `svn --version` 可以正常执行。
2. 使用 SVN CLI 或 TortoiseSVN，在当前 Windows 用户下完成一次只读认证。
3. 启动服务，在“系统配置”的“运行模式”开关中选择 `CLI`。
4. 页面提示保存成功后重启服务，使所有依赖 Provider 的后台服务统一切换到 CLI。
5. 在“系统配置”页填写 SVN 仓库地址并配置可用端点。
6. 进入“版本对比”，选择左右端点并启动只读比对。

`config/settings.json` 包含本机地址和端点注册信息，已被 `.gitignore` 忽略。不要在其中保存密码、Token，也不要把真实配置强制提交到 Git。认证由当前 Windows 用户的 SVN 认证缓存提供。

## 开发模式与 Replay

要启用 Demo 和离线 Replay，将 `config/settings.json` 的 `web` 节点配置为以下对象（保留文件中的其他顶层配置）：

```json
{
  "host": "127.0.0.1",
  "port": 5566,
  "dev_mode": true
}
```

重启服务后访问 <http://127.0.0.1:5566/compare/replay>，可加载仓库内的 `var/m2-fixtures/d3c-be317423.m2fixture`。Replay 不访问 SVN。

## 验证

运行全部自动化测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

前端脚本静态检查（需要 Node.js）：

```powershell
node --check app/static/compare.js
node --check app/static/compare_results.js
node --check app/static/compare_results_batch.js
node --check app/static/m2_diff_mapper.js
node --check app/static/offline_replay.js
```

## 目录说明

| 路径 | 用途 |
|---|---|
| `app/` | FastAPI API、服务、页面模板和静态资源 |
| `core/` | SVN 只读访问、Excel/CSV 解析与 Diff 语义 |
| `config/` | 可提交的配置模板和 AI 规则；本机 `settings.json` 不提交 |
| `tests/` | 单元、契约和集成测试，以及固定 Excel/CSV 样例 |
| `docs/` | 当前契约、ADR、工作手册和历史归档 |
| `var/m2-fixtures/` | 可提交的冻结 Replay 夹具 |

维护“版本对比”前先阅读 `docs/VERSION-COMPARISON-HANDBOOK.md`。数据契约以 `docs/contracts/`、`docs/adr/ADR-006-m1-head-freeze-table-excel.md` 和 `docs/adr/ADR-007-m2-table-tablecsv-pairing.md` 为准。

## 常见问题

- `SVN_AUTH_FAILED`：当前 Windows 用户还没有可用的 SVN 认证缓存，先通过 SVN CLI 或 TortoiseSVN 完成一次只读访问。
- 页面能打开但没有可选端点：`config/settings.json` 中还没有有效的 `svn.endpoint_registry`，请先在系统配置页保存端点。
- `/compare/replay` 返回 404：`web.dev_mode` 未设为 `true`，或修改配置后没有重启服务。
- 切换 MOCK/CLI 后行为未变化：Provider 在进程启动时注入各项服务，按页面提示重启服务后生效。
