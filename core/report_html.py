"""
HTML 单文件报告生成器。

- 完全自包含：CSS/JS/数据全部内联，无 CDN，离线双击可开。
- 四个 Tab：按人（默认）/ 按表 / 净差异 / 时间线。
- 全局搜索、新旧值并排、字符级差异高亮、盲区声明页脚。
"""
import json
from datetime import datetime, timezone


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>配置表修改报告</title>
<style>
  :root{ --bg:#f5f6f8; --card:#fff; --line:#e3e6ea; --ink:#222; --mut:#8a929c;
         --red:#d93025; --green:#188038; --blue:#1a73e8; --gray:#9aa0a6; --hl:#fff3bf; }
  *{box-sizing:border-box}
  body{margin:0;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
       background:var(--bg);color:var(--ink);font-size:14px}
  header{padding:18px 24px;background:var(--card);border-bottom:1px solid var(--line)}
  header h1{margin:0 0 4px;font-size:20px}
  header .meta{color:var(--mut);font-size:12px;line-height:1.7}
  .stats{display:flex;gap:12px;flex-wrap:wrap;padding:16px 24px}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:8px;
        padding:12px 18px;min-width:120px}
  .stat b{display:block;font-size:22px;margin-top:2px}
  .stat span{color:var(--mut);font-size:12px}
  .bar{display:flex;gap:8px;padding:0 24px 12px;align-items:center;flex-wrap:wrap}
  .tabs button{background:var(--card);border:1px solid var(--line);border-radius:6px 6px 0 0;
        padding:8px 16px;cursor:pointer;font-size:14px;color:var(--mut)}
  .tabs button.active{color:var(--ink);border-bottom:2px solid var(--blue);font-weight:600}
  #search{margin-left:auto;padding:6px 10px;border:1px solid var(--line);border-radius:6px;min-width:240px}
  .panel{display:none;padding:8px 24px 40px}
  .panel.active{display:block}
  .card{background:var(--card);border:1px solid var(--line);border-radius:8px;margin-bottom:14px;overflow:hidden}
  .card>.hd{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
  .card>.hd .t{font-weight:600;font-size:15px}
  .card>.hd .sub{color:var(--mut);font-size:12px}
  table{width:100%;border-collapse:collapse}
  th,td{padding:7px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
  th{background:#fafbfc;color:var(--mut);font-weight:600;font-size:12px;position:sticky;top:0}
  tr:hover td{background:#fcfdff}
  .old{color:var(--green)} .new{color:var(--red)}
  .tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;background:#eef1f4;color:var(--mut);margin-right:4px}
  .tag.add{background:#e6f4ea;color:var(--green)} .tag.del{background:#f1f3f4;color:var(--gray)}
  .tag.mod{background:#fce8e6;color:var(--red)} .tag.col{background:#e8f0fe;color:var(--blue)}
  .grp{margin:10px 0;padding-left:10px;border-left:3px solid var(--line)}
  .grp>.gt{font-weight:600;margin:6px 0;color:#3c4043}
  .err{color:var(--red)} .foot{padding:14px 24px;color:var(--mut);font-size:12px;border-top:1px solid var(--line)}
  .empty{color:var(--mut);padding:30px;text-align:center}
  .val{max-width:360px;word-break:break-all}
  mark{background:var(--hl);padding:0 1px}
</style>
</head>
<body>
<header>
  <h1>配置表修改报告</h1>
  <div class="meta" id="meta"></div>
</header>
<div class="stats" id="stats"></div>
<div class="bar">
  <div class="tabs">
    <button data-tab="person" class="active">按人</button>
    <button data-tab="table">按表</button>
    <button data-tab="net">净差异</button>
    <button data-tab="timeline">时间线</button>
  </div>
  <input id="search" placeholder="搜索 人名 / 表名 / 字段 / 值…">
</div>
<div class="panel active" id="panel-person"></div>
<div class="panel" id="panel-table"></div>
<div class="panel" id="panel-net"></div>
<div class="panel" id="panel-timeline"></div>
<div class="foot" id="foot"></div>

<script id="data" type="application/json">__DATA__</script>
<script id="metaj" type="application/json">__META__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const META = JSON.parse(document.getElementById('metaj').textContent);

// ---- 字符级差异高亮 (LCS) ----
function lcsHi(a,b){
  if(a===b) return esc(a);
  if(!a) return '<span class="new">'+esc(b)+'</span>';
  if(!b) return '<span class="old">'+esc(a)+'</span>';
  const n=a.length,m=b.length;
  const dp=Array.from({length:n+1},()=>new Array(m+1).fill(0));
  for(let i=n-1;i>=0;i--)for(let j=m-1;j>=0;j--)
    dp[i][j]=a[i]===b[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);
  let i=0,j=0,out='';
  while(i<n&&j<m){
    if(a[i]===b[j]){out+=esc(a[i]);i++;j++;}
    else if(dp[i+1][j]>=dp[i][j+1]){out+='<span class="old">'+esc(a[i])+'</span>';i++;}
    else{out+='<span class="new">'+esc(b[j])+'</span>';j++;}
  }
  while(i<n){out+='<span class="old">'+esc(a[i++])+'</span>';}
  while(j<m){out+='<span class="new">'+esc(b[j++])+'</span>';}
  return out;
}
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function tag(t){const m={cell_modified:['mod','改值'],row_added:['add','增行'],row_deleted:['del','删行'],column_added:['col','增列'],column_removed:['col','删列'],file_added:['add','新增文件'],file_deleted:['del','删除文件']};const[x,y]=m[t]||['',''];return '<span class="tag '+x+'">'+y+'</span>';}

let FILTER='';
function matchItem(c){
  if(!FILTER) return true;
  const f=FILTER.toLowerCase();
  return [c.author,c.workbook,c.sheet,c.column_code,c.column_display,c.old_value,c.new_value,c.message]
    .some(v=>(v||'').toLowerCase().includes(f));
}

function rowOf(c){
  return '<tr>'+
    '<td>'+esc(c.revision)+'</td>'+
    '<td>'+esc(c.row_key)+'</td>'+
    '<td>'+tag(c.change_type)+esc(c.column_display||c.column_code)+'</td>'+
    '<td class="val old">'+lcsHi(c.old_value,'')+'</td>'+
    '<td class="val new">'+lcsHi('',c.new_value)+'</td>'+
    '</tr>';
}
function tableHead(){return '<table><thead><tr><th>版本</th><th>主键</th><th>字段</th><th class="old">旧值</th><th class="new">新值</th></tr></thead><tbody>';}

function renderPerson(){
  const el=document.getElementById('panel-person');
  const bp=DATA.by_person; const keys=Object.keys(bp).sort();
  if(!keys.length){el.innerHTML='<div class="empty">本区间无配置表变更</div>';return;}
  let h='';
  for(const a of keys){
    const items=bp[a].filter(matchItem);
    if(!items.length && FILTER) continue;
    const wbs=[...new Set(items.map(i=>i.workbook))];
    h+='<div class="card"><div class="hd"><span class="t">'+esc(a)+'</span>'+
       '<span class="sub">提交 '+items.length+' 项 · 涉及工作簿 '+wbs.length+' 个</span></div><div class="grp">';
    const byWb={}; items.forEach(i=>(byWb[i.workbook]=byWb[i.workbook]||[]).push(i));
    for(const wb of Object.keys(byWb)){
      h+='<div class="gt">'+esc(wb)+'</div>'+tableHead()+'<tbody>';
      for(const c of byWb[wb]) h+=rowOf(c);
      h+='</tbody></table>';
    }
    h+='</div></div>';
  }
  el.innerHTML=h||'<div class="empty">无匹配</div>';
}

function renderTable(){
  const el=document.getElementById('panel-table');
  const bt=DATA.by_table; const wbs=Object.keys(bt).sort();
  if(!wbs.length){el.innerHTML='<div class="empty">本区间无配置表变更</div>';return;}
  let h='';
  for(const wb of wbs){
    for(const sh of Object.keys(bt[wb]).sort()){
      const items=bt[wb][sh].filter(matchItem);
      if(!items.length && FILTER) continue;
      h+='<div class="card"><div class="hd"><span class="t">'+esc(wb)+' / '+esc(sh)+'</span>'+
         '<span class="sub">'+items.length+' 项变更</span></div>'+tableHead();
      for(const c of items) h+=rowOf(c);
      h+='</table></div>';
    }
  }
  el.innerHTML=h||'<div class="empty">无匹配</div>';
}

function renderNet(){
  const el=document.getElementById('panel-net');
  const net=DATA.net; const names=Object.keys(net.sheets).sort();
  if(!names.length){el.innerHTML='<div class="empty">无净差异</div>';return;}
  let h='';
  for(const nm of names){
    const sd=net.sheets[nm]; const sm=sd.modified_cells||[];
    const adds=sd.added_rows||[]; const dels=sd.removed_rows||[];
    const ca=sd.column_added||[]; const cr=sd.column_removed||[];
    if(!sm.length&&!adds.length&&!dels.length&&!ca.length&&!cr.length) continue;
    h+='<div class="card"><div class="hd"><span class="t">'+esc(nm)+'</span>'+
       '<span class="sub">'+(sm.length+adds.length+dels.length)+' 项净差异</span></div>'+tableHead();
    for(const c of sm) h+=rowOf({revision:'',row_key:c.row_key,change_type:'cell_modified',column_code:c.col,column_display:c.header,old_value:c.old,new_value:c.new});
    for(const r of adds) h+=rowOf({revision:'',row_key:r._key,change_type:'row_added',column_code:'(行)',column_display:'新增行',old_value:'',new_value:'(整行新增)'});
    for(const r of dels) h+=rowOf({revision:'',row_key:r._key,change_type:'row_deleted',column_code:'(行)',column_display:'删除行',old_value:'(整行删除)',new_value:''});
    for(const c of ca) h+=rowOf({revision:'',row_key:'',change_type:'column_added',column_code:c,column_display:c,old_value:'',new_value:'<新增列>'});
    for(const c of cr) h+=rowOf({revision:'',row_key:'',change_type:'column_removed',column_code:c,column_display:c,old_value:'<删除列>',new_value:''});
    h+='</table></div>';
  }
  el.innerHTML=h||'<div class="empty">所有变更均被后续改动抵消，无净差异</div>';
}

function renderTimeline(){
  const el=document.getElementById('panel-timeline');
  if(!DATA.timeline.length){el.innerHTML='<div class="empty">无提交</div>';return;}
  let h='';
  for(const t of DATA.timeline.slice().reverse()){
    h+='<div class="card"><div class="hd"><span class="t">r'+esc(t.revision)+'</span>'+
       '<span class="sub">'+esc(t.author)+' · '+esc(t.date)+'</span>'+
       '<span class="sub">变更 '+t.change_count+' 项 · 文件 '+t.file_count+' · 表 '+t.sheets.length+'</span></div>'+
       '<div style="padding:8px 14px;color:#3c4043">'+esc(t.message||'(无日志)')+'</div>'+
       (t.sheets.length?'<div style="padding:0 14px 10px;color:var(--mut);font-size:12px">'+t.sheets.map(esc).join(' · ')+'</div>':'')+
       '</div>';
  }
  el.innerHTML=h;
}

function renderAll(){renderPerson();renderTable();renderNet();renderTimeline();}
function setMeta(){
  document.getElementById('meta').innerHTML =
    '分支：'+esc(META.repo_url)+'<br>版本区间：r'+esc(META.rev_from)+' → r'+esc(META.rev_to)+
    '<br>生成时间：'+esc(META.generated_at);
  const s=DATA.stats||{};
  document.getElementById('stats').innerHTML = [
    ['提交数', s.revisions||0],['参与人数', s.author_count||0],
    ['涉及工作簿', s.workbook_count||0],['变更单元格', s.cell_modified||0],
    ['增行', s.row_added||0],['删行', s.row_deleted||0]
  ].map(([t,v])=>'<div class="stat"><span>'+t+'</span><b>'+v+'</b></div>').join('');
  document.getElementById('foot').innerHTML = '覆盖声明：'+(META.blind_spot||'')+
    (DATA.errors&&DATA.errors.length?'<br>错误：'+DATA.errors.map(esc).join('；'):'');
}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  b.classList.add('active');
  document.getElementById('panel-'+b.dataset.tab).classList.add('active');
});
document.getElementById('search').oninput=e=>{FILTER=e.target.value.trim();renderAll();};
setMeta(); renderAll();
</script>
</body>
</html>
"""


def render(result: dict, meta: dict) -> str:
    meta = dict(meta)
    meta.setdefault("generated_at", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    meta.setdefault("blind_spot",
                    "本报告基于导出 CSV 生成；CSV 未包含的列（导出端=None，如中文备注/作者列）其改动不会被检出。")
    html = HTML_TEMPLATE
    html = html.replace("__DATA__", json.dumps(result, ensure_ascii=False))
    html = html.replace("__META__", json.dumps(meta, ensure_ascii=False))
    return html
