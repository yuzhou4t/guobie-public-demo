"""Exercise the event overview's confirmed-source report rendering."""

import subprocess
from pathlib import Path


def test_event_overview_lists_each_confirmed_source_report_and_original_link():
    script = Path(__file__).resolve().parents[1] / "app/static/reader/app.js"
    program = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('function eventSourceReportsHtml(');
const dialogStart=source.indexOf('function eventSourceReportsDialogHtml(',start+1);
const end=source.indexOf('\nfunction ',dialogStart+1);
assert(start>=0 && dialogStart>start && end>dialogStart);
const sources=Array.from({length:4},(_,index)=>({
  mention_id:index+1,
  mention_summary:`命中摘要 ${index+1}`,
  evidence_locator:{page:index+2},
  times:{reported_at:`2026-08-${20+index}T00:00:00Z`,recorded_at:'2026-08-25T00:00:00Z'},
  document:{source_id:index+1,source_name:`来源 ${index+1}`,document_version_id:101+index,
    version_no:1,title:`原始报告 ${index+1}`,document_type:'report',
    published_at:`2026-08-${20+index}T00:00:00Z`,published_at_precision:'day',
    abstract:`来源摘要 ${index+1}`,canonical_url:`https://example.test/report-${index+1}`}
}));
const context=vm.createContext({
  sources,
  escapeHtml:String,safeUrl:String,publishedLabel:value=>value.slice(0,10),localDate:value=>value.slice(0,10),
  formatLocator:value=>`页码：${value.page}`,displayEnum:(_labels,_value,fallback)=>fallback,
  DOCUMENT_TYPE_LABELS:{},projectMaterialAttributes:()=>'',emptyState:String,Number,Map,
});
const functions=source.slice(start,end);
const trigger=vm.runInContext(functions+'\neventSourceReportsHtml(sources,4)',context);
assert.match(trigger,/data-open-event-source-reports/);
assert.match(trigger,/4 个来源 · 4 份原始报告/);
assert.doesNotMatch(trigger,/原始报告 1/);
const html=vm.runInContext(functions+'\neventSourceReportsDialogHtml(sources,4)',context);
assert.match(html,/4 个确认来源 · 4 份原始报告/);
for(let index=1;index<=4;index++){
  assert.match(html,new RegExp(`来源 ${index}`));
  assert.match(html,new RegExp(`原始报告 ${index}`));
  assert.match(html,new RegExp(`data-material-version="${100+index}"`));
  assert.match(html,new RegExp(`https://example.test/report-${index}`));
}
assert.match(html,/来源摘要/);
assert.match(html,/事件命中摘要/);
assert.match(html,/证据定位/);
"""
    result = subprocess.run(["node", "-e", program, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
