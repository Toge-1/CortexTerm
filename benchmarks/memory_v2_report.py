"""Render self-contained, inspectable experiment results; no network or model calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CortexTerm · Memory Lab</title>
<style>
:root{color-scheme:dark;--bg:#101418;--panel:#192126;--line:#314047;--fg:#e9efeb;--muted:#a1b0b1;--green:#9ae8ba;--red:#f3a298;--gold:#f0ce8e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:1320px;padding:38px 36px 70px;margin:auto}header{display:flex;justify-content:space-between;gap:32px;align-items:flex-start;border-bottom:1px solid var(--line);padding-bottom:26px}
.eyebrow{color:var(--green);font:12px monospace;letter-spacing:2px}h1{font-size:38px;line-height:1.2;margin:12px 0}h2{font-size:20px;margin:0 0 12px}p{margin:8px 0}small,.muted{color:var(--muted)}code{font-family:Consolas,monospace;color:var(--green)}
.stamp{font-size:12px;text-align:right;min-width:210px}.note{border-left:3px solid var(--gold);padding:12px 18px;background:#24241f;margin:24px 0;color:#e4d8bc}
.grid{display:grid;grid-template-columns:1.3fr 1fr;gap:20px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px;margin-bottom:20px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--muted);font-weight:400}td,th{padding:12px 10px;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:0}
.bar{height:6px;background:#304039;border-radius:6px;margin-top:5px;max-width:130px}.bar i{display:block;height:100%;background:var(--green);border-radius:6px}.good{color:var(--green)}.bad{color:var(--red)}
.metrics{display:flex;gap:32px;flex-wrap:wrap}.metric strong{display:block;font-size:28px}.metric span{color:var(--muted);font-size:12px}
nav{display:flex;gap:10px;margin:24px 0 18px}button,select{font:inherit;background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:8px 14px;cursor:pointer}button.active{background:var(--green);color:#10251b}
.flow{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;gap:10px;align-items:center}.node{border:1px solid var(--line);border-radius:8px;padding:14px}.node strong{display:block}.node small{display:block}.arrow{color:var(--green)}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.55 Consolas,monospace;background:#101719;border:1px solid var(--line);border-radius:6px;padding:14px;max-height:480px;overflow:auto}
details{border-top:1px solid var(--line);padding:12px 0}summary{cursor:pointer}.filters{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}.tag{display:inline-block;font:11px monospace;background:#27383b;border-radius:4px;padding:2px 7px;margin-right:8px}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.memory{margin:0}.memory p{white-space:pre-wrap}.memory blockquote{margin:10px 0;padding-left:12px;border-left:2px solid var(--green);font-size:13px;color:var(--muted)}
.hidden{display:none!important}footer{color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:18px;margin-top:22px}
@media(max-width:850px){main{padding:22px 16px}.grid,.cards{grid-template-columns:1fr}header{display:block}.stamp{text-align:left;margin-top:16px}.flow{grid-template-columns:1fr}.arrow{display:none}h1{font-size:30px}table{font-size:12px}td,th{padding:8px 4px}}
</style>
<main>
<header><div><div class="eyebrow">CORTEXTERM / MEMORY LAB</div><h1>记住，找到，再验证。</h1>
<p class="muted">对比五种记忆策略，保留每次改动、执行结果与召回证据。</p></div>
<div class="stamp">模型 <code id="model"></code><br>冻结代码 <code id="commit"></code><br><span id="sample"></span></div></header>
<div class="note">这是自建的编码迁移小样本实验，测试由项目作者编写。结果能验证这批任务的行为，不能替代真实仓库基准，更不能证明复杂记忆普遍优于完整历史或直接搜索。</div>
<div class="note hidden" id="audit-notice"></div>
<div class="panel"><div class="flow">
<div class="node"><strong>01 / 公开事件</strong><small>用户请求、工具结果、公开回复<br>稳定 ID + 时间 + 作用域</small></div><span class="arrow">→</span>
<div class="node"><strong>02 / 经验与证据</strong><small>可选模型提取 · 引文校验<br>新结论修正旧记录，保留出处</small></div><span class="arrow">→</span>
<div class="node"><strong>03 / 按任务召回</strong><small>原文 / 关键词 / 时间 / 可选关联<br>受预算限制，并用代码测试验收</small></div>
</div></div>
<nav><button class="active" data-tab="overview">实验总览</button><button data-tab="runs">逐次执行</button><button data-tab="memories">经验与证据</button><button data-tab="repository">真实仓库回放</button></nav>
<section id="overview">
<div class="grid"><div class="panel"><h2>完成任务 ≠ 复杂设计带来的提升</h2><table><thead><tr><th>策略</th><th>通过</th><th>调用</th><th>平均秒</th></tr></thead><tbody id="scores"></tbody></table></div>
<div class="panel"><h2>额外提取成本</h2><div class="metrics" id="extraction"></div><p class="muted">提取发生在历史阶段，成本单独列出。原文搜索和完整历史不需要这部分模型调用。</p><hr style="border:0;border-top:1px solid var(--line);margin:20px 0">
<h2>控制条件</h2><p>同一模型、同一历史、同一任务与初始代码；每次恢复工作区，交换运行顺序。</p><p>检索不接触隐藏断言；模型只能使用项目读写工具；完成后由独立 Python 进程验证结果。</p><p class="muted">没有使用 LLM judge。重复运行是同一任务的重复测量，不等于更多独立任务。</p></div></div>
<div class="panel"><h2>按任务查看</h2><table><thead id="matrix-head"></thead><tbody id="matrix"></tbody></table></div>
<div class="panel"><h2>关联扩展是否真的参与了？</h2><p id="graph-observation"></p></div>
<div class="panel"><h2>模型用量</h2><table><thead><tr><th>策略</th><th>input_tokens</th><th>cache_read_input_tokens</th><th>output_tokens</th></tr></thead><tbody id="costs"></tbody></table><small>保留服务端原始计量字段；不假定各提供方字段可直接相加，也不套用未经核实的价格。</small></div>
</section>
<section id="runs" class="hidden"><div class="filters"><select id="case-filter"></select><select id="mode-filter"></select></div><div id="run-list"></div></section>
<section id="memories" class="hidden"><p class="muted">这些经验由真实模型从历史材料中提取。引文有出处不代表结论一定正确；查看过时状态与适用条件。</p><div id="memory-list" class="cards"></div></section>
<section id="repository" class="hidden"><div class="note">使用 CortexTerm 历史提交中的真实源码及既有回归测试。每题先确认旧版本失败、修复版本通过。历史线索是构造的，只有两题、每组一次；不能作为广泛的修复率提升证据。</div><div id="repo-results"></div></section>
<footer>所有数据嵌入本页，无外部请求。完整证据在同目录 runs.jsonl、report.json、cases.frozen.json 与各任务 memories.json。</footer>
</main><script>
const data=__DATA__;
const labels={none:"无记忆",full:"完整历史",raw:"原文搜索",hybrid:"提取经验 + 搜索",graph:"提取经验 + 关联扩展"};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=n=>Number(n||0).toLocaleString("en-US");
const report=data.report, manifest=report.manifest, rows=data.rows, modes=manifest.modes;
if(report.checker_audit){const box=document.getElementById("audit-notice");box.classList.remove("hidden");box.textContent="判定复核：审批测试原先只接受删除字段，错误地拒绝了把旧值清为 None 的正确方案。已对所有组的原有代码统一重判，没有重新调用模型答题；原始报告和结果仍保留。下表使用复核后的成绩。"}
document.getElementById("model").textContent=manifest.model;
document.getElementById("commit").textContent=manifest.source_commit.slice(0,8);
document.getElementById("sample").textContent=`${manifest.cases.length} 个任务 × ${manifest.repeats} 次 × ${modes.length} 组`;
document.getElementById("scores").innerHTML=modes.map(m=>{let x=report.summary[m];return `<tr><td>${labels[m]}</td><td><strong>${x.passed}/${x.runs}</strong><div class="bar"><i style="width:${x.runs?100*x.passed/x.runs:0}%"></i></div></td><td>${x.model_calls}</td><td>${(x.seconds/x.runs).toFixed(1)}</td></tr>`}).join("");
const calls=report.extraction_calls;
const failedJobs=manifest.cases.reduce((s,c)=>s+(rows.find(r=>r.case===c)?.extraction_status?.jobs?.failed||0),0);
document.getElementById("extraction").innerHTML=[[calls.length,"提取调用"],[failedJobs,"提取失败任务"],[calls.reduce((s,c)=>s+c.seconds,0).toFixed(0),"提取总秒数"]].map(([n,l])=>`<div class="metric"><strong>${n}</strong><span>${l}</span></div>`).join("");
document.getElementById("matrix-head").innerHTML="<tr><th>任务</th>"+modes.map(m=>`<th>${labels[m]}</th>`).join("")+"</tr>";
document.getElementById("matrix").innerHTML=manifest.cases.map(c=>"<tr><td>"+esc(c)+"</td>"+modes.map(m=>{let a=rows.filter(r=>r.case===c&&r.mode===m);let p=a.filter(r=>r.passed).length;return `<td class="${p===a.length?'good':'bad'}">${p}/${a.length}</td>`}).join("")+"</tr>").join("");
const graphRuns=rows.filter(r=>r.mode==="graph"),expanded=graphRuns.filter(r=>r.retrieval_trace?.channels?.graph?.length).length;
document.getElementById("graph-observation").textContent=`${graphRuns.length} 次图模式执行中，${expanded} 次返回了关联候选。`+(expanded===0?" 本数据集未实际检验关联扩展的增益，耗时或分数差异不能归功于图。":" 仍需要更多独立任务判断关联候选是否帮助了修复。");
document.getElementById("costs").innerHTML=modes.map(m=>{let r=report.summary[m];return `<tr><td>${labels[m]}</td><td>${fmt(r.input_tokens)}</td><td>${fmt(r.cache_read_tokens)}</td><td>${fmt(r.output_tokens)}</td></tr>`}).join("");
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{document.querySelectorAll("nav button").forEach(x=>x.classList.toggle("active",x===b));["overview","runs","memories","repository"].forEach(id=>document.getElementById(id).classList.toggle("hidden",id!==b.dataset.tab))});
const cf=document.getElementById("case-filter"),mf=document.getElementById("mode-filter");
cf.innerHTML='<option value="">全部任务</option>'+manifest.cases.map(c=>`<option>${esc(c)}</option>`).join("");
mf.innerHTML='<option value="">全部策略</option>'+modes.map(m=>`<option value="${m}">${labels[m]}</option>`).join("");
function runs(){document.getElementById("run-list").innerHTML=rows.filter(r=>(!cf.value||r.case===cf.value)&&(!mf.value||r.mode===mf.value)).map(r=>`<article class="panel"><h2><span class="${r.passed?'good':'bad'}">${r.passed?'PASS':'FAIL'}</span> · ${esc(r.case)}</h2><p class="muted">${labels[r.mode]} · 第 ${r.repeat+1} 次 · ${r.seconds}s · ${r.requests.length} 次调用 · ${r.context_tokens} 估算记忆 tokens</p><details><summary>执行结果</summary><pre>${esc(r.detail||"全部隐藏断言通过")}</pre></details><details><summary>实际注入的记忆</summary><pre>${esc(r.context||"未注入")}</pre></details><details><summary>召回通道与证据 ID</summary><pre>${esc(JSON.stringify(r.retrieval_trace,null,2))}</pre></details><details><summary>模型写出的代码</summary>${Object.entries(r.files).map(([n,c])=>`<p><code>${esc(n)}</code></p><pre>${esc(c)}</pre>`).join("")}</details><details><summary>工具事件</summary><pre>${esc(JSON.stringify(r.tool_events,null,2))}</pre></details></article>`).join("")}
cf.onchange=mf.onchange=runs;runs();
document.getElementById("memory-list").innerHTML=Object.entries(data.memories).flatMap(([c,ms])=>ms.map(m=>`<article class="panel memory"><small>${esc(c)}</small><p><span class="tag">${esc(m.kind)}</span><span class="${m.status==='active'?'good':'bad'}">${esc(m.status)}</span></p><p>${esc(m.statement)}</p><details><summary>来源 / ${m.evidence.length} 条引文</summary>${m.evidence.map(e=>`<small><code>${esc(e.event_id)}</code></small><blockquote>${esc(e.quote)}</blockquote>`).join("")}</details><small>${esc(m.keywords.join(" · "))}</small></article>`)).join("");
document.getElementById("repo-results").innerHTML=data.repo?.rows?.length?data.repo.rows.map(r=>`<article class="panel"><h2><span class="${r.passed?'good':'bad'}">${r.passed?'PASS':'FAIL'}</span> · ${esc(r.case)} · ${labels[r.mode]}</h2><p class="muted">基准版本 ${esc(r.base_commit.slice(0,8))} · ${r.seconds}s · ${r.requests.length} 次调用</p><details><summary>既有测试执行结果</summary><pre>${esc(r.detail)}</pre></details><details><summary>实际记忆</summary><pre>${esc(r.context||"无")}</pre></details><details><summary>最终源码</summary><pre>${esc(r.source)}</pre></details><details><summary>工具执行顺序</summary><pre>${esc(JSON.stringify(r.tool_events,null,2))}</pre></details></article>`).join(""):'<p class="muted">此报告未附加仓库回放结果。</p>';
</script></html>"""


def render(directory: Path, repo_directory: Path | None = None) -> Path:
    audited = (directory / "report.audited.json").exists()
    report = json.loads((directory / ("report.audited.json" if audited else "report.json")).read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            (directory / ("runs.audited.jsonl" if audited else "runs.jsonl")).read_text(encoding="utf-8").splitlines()]
    memories = {
        path.name.removesuffix(".memories.json"): json.loads(path.read_text(encoding="utf-8"))
        for path in directory.glob("*.memories.json")
    }
    repo = {}
    if repo_directory:
        repo = {
            "report": json.loads((repo_directory / "report.json").read_text(encoding="utf-8")),
            "rows": [json.loads(path.read_text(encoding="utf-8"))
                     for path in sorted(repo_directory.glob("*/*.json")) if path.name != "provenance.json"],
        }
    payload = json.dumps({"report": report, "rows": rows, "memories": memories, "repo": repo}, ensure_ascii=False)
    payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    target = directory / "index.html"
    target.write_text(TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--repo-dir", type=Path)
    args = parser.parse_args()
    print(render(args.directory, args.repo_dir))
