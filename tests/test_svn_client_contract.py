"""
离线契约测试：mock 掉 SVN 网络层，验证 core/svn_client.fetch_revisions 产出的
revisions 结构能被 core/attributor + core/report_html 正确消费。

mock 场景（微型仓库）：
- r100 基线：table/A.csv (Id,HP: 1,100 / 2,200)
- r101 策划A：A.csv 把 Id=1 的 HP 改为 999(M) + 新增 table/B.csv(A)
- r105 策划B：A.csv 把 HP 改回 100(M)  —— 演示「改了又改回」净差异归零

同时验证：(path,rev) 缓存生效，二次运行不再发起 cat 网络调用。

运行：python tests/test_svn_client_contract.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import svn_client, attributor, report_html

URL = "svn://mock/fix"

INFO_XML = """<?xml version="1.0"?>
<info><entry revision="105" path="fix">
<url>svn://mock/fix</url>
<repository><root>svn://mock</root></repository>
<commit revision="105"><author>dev</author><date>2026-07-25T00:00:00Z</date></commit>
</entry></info>"""

LIST_XML = """<?xml version="1.0"?>
<lists><list>
<entry kind="file"><name>table/A.csv</name><commit revision="100"/></entry>
</list></lists>"""

LOG_XML = """<?xml version="1.0"?>
<log>
<logentry revision="101">
<author>策划A</author><date>2026-07-20T10:00:00Z</date><msg>改A并加B</msg>
<paths>
<path action="M">/fix/table/A.csv</path>
<path action="A">/fix/table/B.csv</path>
</paths>
</logentry>
<logentry revision="105">
<author>策划B</author><date>2026-07-25T14:00:00Z</date><msg>改回A</msg>
<paths>
<path action="M">/fix/table/A.csv</path>
</paths>
</logentry>
</log>"""

A_BASE = b"Id,HP\n1,100\n2,200\n"
A_101 = b"Id,HP\n1,999\n2,200\n"
A_105 = b"Id,HP\n1,100\n2,200\n"
B_101 = b"Id,ATK\n1,50\n"

CONTENT = {
    (f"{URL}/table/A.csv@100", "100"): A_BASE,
    (f"{URL}/table/A.csv@101", "101"): A_101,
    (f"{URL}/table/B.csv@101", "101"): B_101,
    (f"{URL}/table/A.csv@105", "105"): A_105,
}


def make_mock():
    calls = {"cat": 0}

    def fake_run(*args, **kw):
        a = args
        if a[0] == "info":
            return (0, INFO_XML, "")
        if a[0] == "list":
            return (0, LIST_XML, "")
        if a[0] == "log":
            return (0, LOG_XML, "")
        return (-1, "", "unexpected " + str(a[0]))

    def fake_run_raw(*args, **kw):
        a = args
        if a[0] == "cat":
            calls["cat"] += 1
            return (0, CONTENT[(a[3], a[2])], "")
        return (-1, b"", "unexpected " + str(a[0]))

    return fake_run, fake_run_raw, calls


def main():
    fake_run, fake_run_raw, calls = make_mock()
    svn_client._run = fake_run
    svn_client._run_raw = fake_run_raw

    tmp = tempfile.mkdtemp(prefix="svn_cache_")
    client = svn_client.SVNClient(cache_dir=tmp)

    revisions, meta = client.fetch_revisions(URL, 100, 105)
    cat_count_1 = calls["cat"]

    # 结构校验
    assert revisions[0]["revision"] == 100 and revisions[0]["author"] is None, "baseline 必须是第一条且 author=None"
    assert "table/A.csv" in revisions[0]["files"], "baseline 应含 A.csv"
    assert len(revisions) == 3, f"应为 3 条 revision（基线+2提交），实际 {len(revisions)}"
    assert revisions[1]["author"] == "策划A" and revisions[2]["author"] == "策划B"

    result = attributor.Attributor().run(revisions)
    s = result["stats"]

    # A3 类比：改了又改回 -> 明细各 1 条 cell_modified，净差异 0
    assert s["cell_modified"] == 2, f"单元格变更应为 2（A改+B改回），实际 {s['cell_modified']}"
    assert s["row_added"] == 1, f"增行应为 1（B.csv 新增），实际 {s['row_added']}"
    assert result["net"]["summary"]["total_modified_cells"] == 0, \
        f"净差异应为 0（改动被抵消），实际 {result['net']['summary']['total_modified_cells']}"
    assert set(result["by_person"].keys()) == {"策划A", "策划B"}, "参与人应为策划A/策划B"

    # 报告渲染不崩
    html = report_html.render(result, meta)
    assert "配置表修改报告" in html and "__DATA__" not in html, "报告渲染失败"

    # 缓存校验：二次运行不再发起任何 cat
    revisions2, _ = client.fetch_revisions(URL, 100, 105)
    result2 = attributor.Attributor().run(revisions2)
    assert result2["stats"] == s, "二次运行结果应与首次一致"
    assert calls["cat"] == cat_count_1, \
        f"二次运行不应再走网络，cat 调用数应为 {cat_count_1}，实际 {calls['cat']}"

    print("PASS  契约测试全部通过")
    print(f"      cat 网络调用数（首次）: {cat_count_1}  （二次）: {calls['cat'] - cat_count_1}  [缓存命中]")
    print(f"      变更: cell_modified={s['cell_modified']} row_added={s['row_added']} "
          f"net_modified={result['net']['summary']['total_modified_cells']}")
    print(f"      参与人: {s['author_count']}  工作簿: {s['workbook_count']}")


if __name__ == "__main__":
    main()
