# -*- coding: utf-8 -*-
"""验证：SpreadsheetML 中 Number 类型单元格经三路合并写回后的类型保真情况。"""
import sys, os, re, tempfile

SD = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "..", "reference", "smartdiff"))
sys.path.insert(0, SD)
import xml_parser, xml_merger

TPL = '''<?xml version="1.0"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Worksheet ss:Name="S1">
  <Table ss:ExpandedRowCount="4" ss:ExpandedColumnCount="3">
   <Row><Cell><Data ss:Type="String">ID</Data></Cell><Cell><Data ss:Type="String">Name</Data></Cell><Cell><Data ss:Type="String">Atk</Data></Cell></Row>
   <Row><Cell><Data ss:Type="String">1001</Data></Cell><Cell><Data ss:Type="String">剑</Data></Cell><Cell><Data ss:Type="Number">{v1}</Data></Cell></Row>
   <Row><Cell><Data ss:Type="String">1002</Data></Cell><Cell><Data ss:Type="String">盾</Data></Cell><Cell><Data ss:Type="Number">{v2}</Data></Cell></Row>
{extra}  </Table>
 </Worksheet>
</Workbook>
'''

NEWROW = '   <Row><Cell><Data ss:Type="String">1003</Data></Cell><Cell><Data ss:Type="String">弓</Data></Cell><Cell><Data ss:Type="Number">30</Data></Cell></Row>\n'

d = tempfile.mkdtemp()
paths = {}
for name, kw in [("base", dict(v1="10", v2="20", extra="")),
                 ("mine", dict(v1="11", v2="20", extra="")),
                 ("theirs", dict(v1="10", v2="22", extra=NEWROW))]:
    p = os.path.join(d, name + ".xml")
    open(p, "w", encoding="utf-8").write(TPL.format(**kw))
    paths[name] = p

parsed = {k: xml_parser.parse_file(v) for k, v in paths.items()}
res = xml_merger.three_way_diff(parsed["base"], parsed["mine"], parsed["theirs"])
r = xml_merger.apply_resolutions(res, [])
print("自动决议完成 ok=%s  未决议=%d" % (r["ok"], len(r["unresolved"])))

out = os.path.join(d, "merged.xml")
xml_merger.write_merged_xml(paths["mine"], res, out)
merged = open(out, encoding="utf-8-sig").read()

print("\n--- 合并结果中每个 Cell 的 (类型, 值) ---")
for i, row in enumerate(re.findall(r"<Row[^>]*>(.*?)</Row>", merged, re.S)):
    print("  Row%d: %s" % (i + 1, re.findall(r'ss:Type="(\w+)">([^<]*)<', row)))

print("\n判定：")
print("  1002 的 Atk 由远端 20->22 自动合并，应仍为 Number")
print("  1003 是远端新增行，被插入 MINE，观察其 Atk 类型是否退化为 String")
