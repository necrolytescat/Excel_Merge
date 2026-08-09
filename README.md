# Excel Diff/Merge 平台

这是一个面向游戏策划的本地 Web 工具，用于从两个 SVN 端点冻结 HEAD Revision，并对 Table Excel 与同侧 TableCsv 生成版本差异报告。

当前已交付的是只读的“版本对比”流程。项目不会执行 SVN commit、merge、update，也不会修改或写回 Excel 文件。

## 环境要求

- Windows 10/11；
- Python 3.10 或更高版本；
- 访问私有 GitHub 仓库的权限；
- 使用真实数据时，需要安装 SVN CLI（命令 `svn` 可用），并在当前 Windows 用户下完成只读认证。

Node.js 只用于前端 JavaScript 静态检查，不是启动 Web 服务的必需依赖。

## 快速启动

在 PowerShell 中执行：

```powershell
git clone https://github.com/necrolytescat/Excel_Merge.git
Set-Location Excel_Merge

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Copy-Item config\settings.m0.example.json config\settings.json
.\.venv\Scripts\python.exe -m app.main
```

打开 <http://127.0.0.1:5566>。健康检查地址为 <http://127.0.0.1:5566/api/health>，版本对比入口为 <http://127.0.0.1:5566/compare>。

示例配置默认使用 `mock` Provider，适合先确认依赖和页面能正常运行。

## 连接真实 SVN

1. 确认 `svn --version` 可以正常执行。
2. 使用 SVN CLI 或 TortoiseSVN，在当前 Windows 用户下完成一次只读认证。
3. 编辑 `config/settings.json`，将 `svn.provider` 改为 `cli`。
4. 启动服务，在“系统配置”页填写 SVN 仓库地址并配置可用端点。
5. 进入“版本对比”，选择左右端点并启动只读比对。

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
- 修改配置后行为未变化：配置在进程启动时读取，需要重启服务。
