"""Canonical M3 report rendering, publication, references, and retention."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import html
import re
from typing import Callable, Protocol
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from app.schemas.monitor import (
    MonitorChangePayload,
    MonitorChangeType,
    MonitorChangeTypeCountsPayload,
    MonitorCoveragePayload,
    MonitorPublicErrorPayload,
    MonitorReportPayload,
    MonitorReportSummaryPayload,
    MonitorRevisionRangePayload,
    MonitorRunSummaryPayload,
    MonitorTaskPayload,
    MonitorTimeIntervalPayload,
    serialize_monitor_json,
)
from app.services.monitor_report_template import HTML_TEMPLATE_V2 as _HTML_TEMPLATE_V2


REPORT_RETENTION = timedelta(days=30)
REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MANAGED_HISTORY = re.compile(
    r"^(?P<stem>\d{8}-\d{6}(?:-\d{6})?)\.(?P<kind>html|json)$"
)
_REFERENCE = re.compile(r"^m3r_[A-Za-z0-9_-]{22}$")
_EMBEDDED_REPORT = re.compile(
    rb'<script type="application/json" id="report-data">(?P<data>.*?)</script>',
    re.DOTALL,
)


class MonitorReportPublishError(RuntimeError):
    """A report artifact could not be safely published."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class MonitorReportReferenceError(ValueError):
    """A report reference is invalid, corrupt, or owned elsewhere."""


@dataclass(frozen=True)
class ReportDraft:
    payload: MonitorReportPayload
    canonical_json: bytes
    offline_html: bytes
    report_ref: str
    json_sha256: str
    html_sha256: str
    report_expires_at: datetime

    @property
    def status(self) -> str:
        return self.payload.status

    @property
    def run_summary(self) -> MonitorRunSummaryPayload:
        summary = self.payload.summary
        return MonitorRunSummaryPayload(
            workbook_count=summary.workbook_count,
            changed_workbook_count=summary.changed_workbook_count,
            change_count=summary.change_count,
            error_count=summary.error_count,
        )


@dataclass(frozen=True)
class ReportPublication:
    status: str
    start_revision: int
    end_revision: int
    run_summary: MonitorRunSummaryPayload
    report_ref: str
    report_sha256: str
    report_expires_at: datetime
    errors: tuple[MonitorPublicErrorPayload, ...]
    html_sha256: str | None = None


@dataclass(frozen=True)
class ResolvedMonitorReport:
    payload: MonitorReportPayload
    canonical_json: bytes
    offline_html: bytes
    json_sha256: str
    html_sha256: str


class MonitorReportPublisher(Protocol):
    def render(
        self,
        *,
        run_id: str,
        task: MonitorTaskPayload,
        interval: MonitorTimeIntervalPayload,
        start_revision: int,
        end_revision: int,
        workbook_count: int,
        changes: tuple[MonitorChangePayload, ...],
        errors: tuple[MonitorPublicErrorPayload, ...],
        generated_at: datetime,
    ) -> ReportDraft: ...

    def publish_history(
        self,
        draft: ReportDraft,
        *,
        ensure_owned: Callable[[], None] | None = None,
    ) -> ReportPublication: ...

    def activate_latest(
        self,
        draft: ReportDraft,
        *,
        ensure_owned: Callable[[], None] | None = None,
    ) -> None: ...


def report_reference(report_id: UUID) -> str:
    token = base64.urlsafe_b64encode(report_id.bytes).decode("ascii").rstrip("=")
    return "m3r_" + token


def parse_report_reference(reference: str) -> UUID:
    if not _REFERENCE.fullmatch(reference):
        raise MonitorReportReferenceError("invalid report reference")
    try:
        report_id = UUID(bytes=base64.urlsafe_b64decode(reference[4:] + "=="))
    except (ValueError, TypeError) as error:
        raise MonitorReportReferenceError("invalid report reference") from error
    if report_reference(report_id) != reference:
        raise MonitorReportReferenceError("non-canonical report reference")
    return report_id


def _change_key(change: MonitorChangePayload) -> tuple[str, ...]:
    return (
        change.workbook.casefold(),
        change.workbook,
        change.sheet_name.casefold(),
        change.sheet_name,
        change.row_key or "",
        change.change_type.value,
        change.field_name or "",
    )


def _stable_change(change: MonitorChangePayload) -> MonitorChangePayload:
    updates = {}
    for side_name in ("source", "target"):
        side = getattr(change, side_name)
        if side is not None and side.row_values is not None:
            ordered = {
                key: side.row_values[key]
                for key in sorted(
                    side.row_values, key=lambda value: (value.casefold(), value)
                )
            }
            updates[side_name] = side.model_copy(update={"row_values": ordered})
    return change.model_copy(update=updates) if updates else change


def _error_key(error: MonitorPublicErrorPayload) -> tuple[str, ...]:
    return (
        error.workbook or "",
        error.sheet_name or "",
        error.stage.value,
        error.code.value,
        error.message,
    )


def build_monitor_report(
    *,
    run_id: str,
    task: MonitorTaskPayload,
    interval: MonitorTimeIntervalPayload,
    start_revision: int,
    end_revision: int,
    workbook_count: int,
    changes: tuple[MonitorChangePayload, ...],
    errors: tuple[MonitorPublicErrorPayload, ...],
    generated_at: datetime,
) -> MonitorReportPayload:
    stable_changes = tuple(
        sorted((_stable_change(change) for change in changes), key=_change_key)
    )
    stable_errors = tuple(sorted(errors, key=_error_key))
    counts = {kind.value: 0 for kind in MonitorChangeType}
    for change in stable_changes:
        counts[change.change_type.value] += 1
    changed_workbooks = {change.workbook for change in stable_changes}
    changed_sheets = {(change.workbook, change.sheet_name) for change in stable_changes}
    changed_rows = {
        (change.workbook, change.sheet_name, change.row_key)
        for change in stable_changes
        if change.row_key is not None
    }
    changed_fields = {
        (change.workbook, change.sheet_name, change.row_key, change.field_name)
        for change in stable_changes
        if change.field_name is not None
    }
    known_authors = {
        change.attribution.author
        for change in stable_changes
        if change.attribution.status == "attributed"
    }
    unknown_authors = sum(
        change.attribution.status == "unknown_author" for change in stable_changes
    )
    unresolved = sum(
        change.attribution.status == "unresolved" for change in stable_changes
    )
    failed_workbooks = {
        error.workbook for error in errors if error.workbook is not None
    }
    report_run_id = UUID(run_id)
    return MonitorReportPayload(
        report_id=uuid5(report_run_id, "m3.monitor-report.v1"),
        run_id=report_run_id,
        task_id=task.task_id,
        task_name=task.name,
        status="partial" if errors or unresolved else "succeeded",
        branch=task.branch,
        interval=interval,
        revisions=MonitorRevisionRangePayload(
            start_revision=start_revision,
            end_revision=end_revision,
        ),
        generated_at=max(generated_at.astimezone(timezone.utc), interval.end_at),
        summary=MonitorReportSummaryPayload(
            workbook_count=workbook_count,
            changed_workbook_count=len(changed_workbooks),
            sheet_count=len(changed_sheets),
            changed_row_count=len(changed_rows),
            changed_field_count=len(changed_fields),
            author_count=len(known_authors),
            change_count=len(stable_changes),
        error_count=len(stable_errors),
            by_change_type=MonitorChangeTypeCountsPayload(**counts),
        ),
        coverage=MonitorCoveragePayload(
            excluded_content=[
                "scope_none_fields",
                "unexported_fields",
                "excel_notes",
                "formulas",
                "styles",
                "macros",
            ],
            unknown_author_count=unknown_authors,
            unattributed_change_count=unresolved,
            failed_workbook_count=len(failed_workbooks),
        ),
        changes=list(stable_changes),
        errors=list(stable_errors),
    )


def _embedded_json(canonical_json: bytes) -> str:
    return (
        canonical_json.decode("utf-8")
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_monitor_report_html(report: MonitorReportPayload) -> bytes:
    """Render a single-file, dependency-free, accessible report workbench."""
    embedded = _embedded_json(serialize_monitor_json(report))
    title = html.escape(f"{report.task_name} - 版本监控报告", quote=True)
    before_data, after_data = _HTML_TEMPLATE_V2.split("__REPORT_DATA__", 1)
    document = (
        before_data.replace("__TITLE__", title, 1)
        + embedded
        + after_data
    )
    return document.encode("utf-8")


def render_legacy_compatible_report_html(raw_html: bytes) -> bytes:
    """Upgrade older report shells in memory without rewriting history files."""
    if b'data-report-template="m3-workbench-v2.1"' in raw_html:
        return raw_html
    return render_monitor_report_html(_decode_embedded_report(raw_html))


def _decode_embedded_report(raw_html: bytes) -> MonitorReportPayload:
    match = _EMBEDDED_REPORT.search(raw_html)
    if match is None:
        raise MonitorReportReferenceError("offline report data is missing")
    try:
        return MonitorReportPayload.model_validate_json(match.group("data"))
    except Exception as error:
        raise MonitorReportReferenceError("offline report data is invalid") from error


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
  <title>__TITLE__</title>
  <style>
    :root {color-scheme:light;--ink:#17202a;--muted:#596573;--line:#cbd3da;--paper:#f5f7f8;--panel:#fff;--accent:#146c60;--warning:#9a351d;--focus:#005fcc}
    *{box-sizing:border-box}html{font-family:"Segoe UI","Microsoft YaHei",sans-serif;font-size:16px;letter-spacing:0;background:var(--paper);color:var(--ink)}
    body{margin:0;min-width:280px}.skip{position:absolute;left:.75rem;top:-5rem;background:#fff;color:#000;padding:.75rem;z-index:20}.skip:focus{top:.75rem}
    :focus-visible{outline:3px solid var(--focus);outline-offset:2px}header{background:#1f2933;color:#fff;border-bottom:4px solid var(--accent)}
    .header-inner,main,footer{width:min(100%,96rem);margin:auto;padding:1rem}h1{font-size:1.35rem;margin:0 0 .75rem;overflow-wrap:anywhere}h2{font-size:1.05rem;margin:0 0 .75rem}
    .meta{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.5rem 1rem;margin:0}.meta div{min-width:0}.meta dt{color:#cbd5df;font-size:.75rem}.meta dd{margin:.15rem 0 0;overflow-wrap:anywhere}
    .status{display:inline-flex;min-height:1.75rem;padding:.2rem .55rem;border:1px solid currentColor;border-radius:4px;font-weight:700}.status.partial{color:#ffe4d6}
    main{display:grid;gap:1rem}.notice{border-left:5px solid var(--warning);background:#fff1eb;padding:.75rem 1rem}.summary{display:grid;grid-template-columns:repeat(6,minmax(7rem,1fr));gap:.5rem}
    .metric{border:1px solid var(--line);background:var(--panel);padding:.65rem;border-radius:4px}.metric strong{display:block;font-size:1.25rem}.metric span,.results{color:var(--muted);font-size:.8rem}
    .filters{display:grid;grid-template-columns:minmax(12rem,2fr) repeat(4,minmax(9rem,1fr));gap:.65rem;padding:.8rem;background:var(--panel);border:1px solid var(--line)}
    label{display:grid;gap:.25rem;color:var(--muted);font-size:.8rem;min-width:0}input,select{width:100%;min-height:2.75rem;padding:.45rem .55rem;border:1px solid #87939f;border-radius:4px;background:#fff;color:var(--ink);font:inherit}
    .results{min-height:1.5rem}.table-wrap{width:100%;overflow:auto;border:1px solid var(--line);background:var(--panel)}table{width:100%;min-width:78rem;border-collapse:collapse;font-size:.82rem}
    caption{text-align:left;padding:.75rem;font-weight:700}th{position:sticky;top:0;z-index:1;text-align:left;background:#e8ecef}th,td{border-bottom:1px solid var(--line);padding:.5rem .55rem;vertical-align:top}
    tbody tr:nth-child(even){background:#fafbfc}td.value{max-width:22rem;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}td.key,td.rev{font-variant-numeric:tabular-nums}
    .empty{padding:2rem 1rem;text-align:center;color:var(--muted)}details{background:var(--panel);border:1px solid var(--line);padding:.75rem}summary{cursor:pointer;font-weight:700}
    .errors{margin:.75rem 0 0;padding-left:1.25rem}.errors li{margin:.4rem 0;overflow-wrap:anywhere}footer{color:var(--muted);font-size:.78rem;padding-top:0}
    .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
    @media(max-width:1023px){.meta{grid-template-columns:repeat(2,minmax(0,1fr))}.summary{grid-template-columns:repeat(3,minmax(0,1fr))}.filters{grid-template-columns:repeat(2,minmax(0,1fr))}.search{grid-column:1/-1}}
    @media(max-width:767px){.header-inner,main,footer{padding:.75rem}.summary{grid-template-columns:repeat(2,minmax(0,1fr))}.filters{grid-template-columns:1fr}.search{grid-column:auto}}
    @media(max-width:359px){.meta,.summary{grid-template-columns:1fr}}@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition-duration:.01ms!important;animation-duration:.01ms!important;animation-iteration-count:1!important}}
    @media print{.skip,.filters{display:none}header{color:#000;background:#fff;border-bottom:2px solid #000}.meta dt{color:#444}.table-wrap{overflow:visible}table{min-width:0;font-size:8pt}th{position:static}}
  </style>
</head>
<body>
  <a class="skip" href="#main-content">跳到报告内容</a>
  <header><div class="header-inner"><h1 id="report-title"></h1><dl class="meta" id="report-meta"></dl></div></header>
  <main id="main-content" tabindex="-1">
    <div id="partial-notice" class="notice" role="alert" hidden>本报告为部分成功，存在未覆盖数据。请结合错误摘要进行回归。</div>
    <section aria-labelledby="summary-title"><h2 id="summary-title">变化摘要</h2><div class="summary" id="summary"></div></section>
    <section aria-labelledby="changes-title">
      <h2 id="changes-title">最终净值变化</h2>
      <form class="filters" id="filters" role="search">
        <label class="search">搜索主键、字段和值<input id="query" type="search" autocomplete="off"></label>
        <label>变化类型<select id="change-type"><option value="">全部</option></select></label>
        <label>修改人<select id="author"><option value="">全部</option></select></label>
        <label>工作簿<select id="workbook"><option value="">全部</option></select></label>
        <label>Sheet<select id="sheet"><option value="">全部</option></select></label>
      </form>
      <p id="result-status" class="results" aria-live="polite" aria-atomic="true"></p>
      <div class="table-wrap" tabindex="0" aria-label="变化明细横向滚动区域">
        <table><caption class="sr-only">版本监控最终净值变化明细</caption><thead><tr>
          <th scope="col">工作簿</th><th scope="col">Sheet</th><th scope="col">变化类型</th><th scope="col">主键</th><th scope="col">字段</th><th scope="col">起始值</th><th scope="col">截止值</th><th scope="col">最终修改人</th><th scope="col">Revision</th><th scope="col">修改时间</th><th scope="col">提交说明</th>
        </tr></thead><tbody id="change-rows"></tbody></table>
      </div>
      <div id="empty" class="empty" hidden>没有符合当前筛选条件的变化。</div>
    </section>
    <details id="error-panel"><summary>公开错误摘要（<span id="error-count">0</span>）</summary><ul class="errors" id="errors"></ul></details>
  </main>
  <footer>覆盖范围：可靠导出的 TableCsv 业务字段；不包含未导出字段、Excel 备注、公式、样式、宏和 scope=none 字段。</footer>
  <script type="application/json" id="report-data">__REPORT_DATA__</script>
  <script>
  (function(){
    "use strict";
    var report=JSON.parse(document.getElementById("report-data").textContent);
    var labels={field_modified:"字段修改",row_added:"新增行",row_deleted:"删除行",field_added:"新增字段",field_removed:"删除字段",field_definition_modified:"字段定义修改"};
    var byId=function(id){return document.getElementById(id)};
    var text=function(tag,value,className){var node=document.createElement(tag);node.textContent=value==null?"-":value;if(className){node.className=className}return node};
    var formatTime=function(value){return value?new Intl.DateTimeFormat("zh-CN",{dateStyle:"medium",timeStyle:"medium",timeZone:"Asia/Shanghai"}).format(new Date(value)):"-"};
    var valueOf=function(side){if(!side){return "-"}if(side.row_values){return Object.entries(side.row_values).map(function(pair){return pair[0]+": "+pair[1]}).join("\\n")}if(side.field_definition){var d=side.field_definition;return "显示名: "+(d.display_name||"-")+"\\n类型: "+d.declared_type+"\\n范围: "+d.scope}return side.display_value};
    byId("report-title").textContent=report.task_name+" - 版本监控报告";
    var meta=[["状态",report.status==="partial"?"部分成功":"成功",report.status],["固定分支",report.branch.label],["分支路径",report.branch.repository_relative_path],["报告区间",formatTime(report.interval.start_at)+" 至 "+formatTime(report.interval.end_at)],["Revision",report.revisions.start_revision+" → "+report.revisions.end_revision],["计划截止",formatTime(report.interval.logical_cutoff_at)],["生成时间",formatTime(report.generated_at)]];
    meta.forEach(function(item){var box=document.createElement("div");box.append(text("dt",item[0]));box.append(text("dd",item[1],item[2]?"status "+item[2]:""));byId("report-meta").append(box)});
    byId("partial-notice").hidden=report.status!=="partial";
    [["变化",report.summary.change_count],["变化行",report.summary.changed_row_count],["变化字段",report.summary.changed_field_count],["工作簿",report.summary.changed_workbook_count],["修改人",report.summary.author_count],["错误",report.summary.error_count]].forEach(function(item){var box=text("div","","metric");box.append(text("strong",String(item[1])),text("span",item[0]));byId("summary").append(box)});
    var controls={query:byId("query"),type:byId("change-type"),author:byId("author"),workbook:byId("workbook"),sheet:byId("sheet")};
    var addOptions=function(select,values,labeler){Array.from(new Set(values)).sort(function(a,b){return a.localeCompare(b,"zh-CN")}).forEach(function(value){var option=text("option",labeler?labeler(value):value);option.value=value;select.append(option)})};
    addOptions(controls.type,report.changes.map(function(item){return item.change_type}),function(value){return labels[value]||value});addOptions(controls.author,report.changes.map(function(item){return item.attribution.author}));addOptions(controls.workbook,report.changes.map(function(item){return item.workbook}));addOptions(controls.sheet,report.changes.map(function(item){return item.sheet_name}));
    var render=function(){var query=controls.query.value.trim().toLocaleLowerCase("zh-CN");var rows=report.changes.filter(function(item){return(!query||JSON.stringify(item).toLocaleLowerCase("zh-CN").includes(query))&&(!controls.type.value||item.change_type===controls.type.value)&&(!controls.author.value||item.attribution.author===controls.author.value)&&(!controls.workbook.value||item.workbook===controls.workbook.value)&&(!controls.sheet.value||item.sheet_name===controls.sheet.value)});byId("change-rows").replaceChildren();rows.forEach(function(item){var tr=document.createElement("tr"),attr=item.attribution;[[item.workbook],[item.sheet_name],[labels[item.change_type]||item.change_type],[item.row_key==null?"-":item.row_key,"key"],[item.display_name||item.field_name||"-"],[valueOf(item.source),"value"],[valueOf(item.target),"value"],[attr.author],[attr.revision==null?"-":attr.revision,"rev"],[formatTime(attr.changed_at)],[attr.commit_message||"-","value"]].forEach(function(cell){tr.append(text("td",String(cell[0]),cell[1]))});byId("change-rows").append(tr)});byId("empty").hidden=rows.length!==0;byId("result-status").textContent="显示 "+rows.length+" / "+report.changes.length+" 项变化"};
    Object.values(controls).forEach(function(control){control.addEventListener(control.tagName==="INPUT"?"input":"change",render)});
    byId("error-count").textContent=String(report.errors.length);byId("error-panel").hidden=report.errors.length===0;report.errors.forEach(function(error){byId("errors").append(text("li",error.message+(error.workbook?" · "+error.workbook:"")+(error.sheet_name?" / "+error.sheet_name:"")))});
    render();
  }());
  </script>
</body>
</html>
"""


def create_report_draft(**kwargs) -> ReportDraft:
    report = build_monitor_report(**kwargs)
    canonical = serialize_monitor_json(report)
    offline_html = render_monitor_report_html(report)
    return ReportDraft(
        payload=report,
        canonical_json=canonical,
        offline_html=offline_html,
        report_ref=report_reference(report.report_id),
        json_sha256=hashlib.sha256(canonical).hexdigest(),
        html_sha256=hashlib.sha256(offline_html).hexdigest(),
        report_expires_at=report.generated_at + REPORT_RETENTION,
    )


def publication_from_draft(draft: ReportDraft) -> ReportPublication:
    report = draft.payload
    return ReportPublication(
        status=report.status,
        start_revision=report.revisions.start_revision,
        end_revision=report.revisions.end_revision,
        run_summary=draft.run_summary,
        report_ref=draft.report_ref,
        report_sha256=draft.json_sha256,
        html_sha256=draft.html_sha256,
        report_expires_at=draft.report_expires_at,
        errors=tuple(report.errors),
    )


class CanonicalJsonReferencePublisher:
    """In-memory publication adapter retained for focused engine tests."""

    def __init__(self):
        self.results: dict[str, bytes] = {}

    def render(self, **kwargs) -> ReportDraft:
        return create_report_draft(**kwargs)

    def publish_history(
        self,
        draft: ReportDraft,
        *,
        ensure_owned: Callable[[], None] | None = None,
    ) -> ReportPublication:
        if ensure_owned is not None:
            ensure_owned()
        self.results[draft.report_ref] = draft.canonical_json
        return publication_from_draft(draft)

    def activate_latest(
        self,
        draft: ReportDraft,
        *,
        ensure_owned: Callable[[], None] | None = None,
    ) -> None:
        if ensure_owned is not None:
            ensure_owned()
        return None

    def publish(self, **kwargs) -> ReportPublication:
        draft = self.render(**kwargs)
        return self.publish_history(draft)
