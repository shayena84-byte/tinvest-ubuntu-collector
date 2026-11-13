"use strict";
// Dependency-free DOM contract tests; Node is needed for CI, not for the runtime.
const fs=require("fs"),path=require("path"),vm=require("vm"),assert=require("assert");
const root=path.resolve(__dirname,"..");
const html=fs.readFileSync(path.join(root,"dashboard.html"),"utf8");
const code=fs.readFileSync(path.join(root,"dashboard.js"),"utf8");
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(x=>x[1]);
assert.equal(ids.length,new Set(ids).size,"HTML IDs must be unique");
assert(!code.includes(".innerHTML"),"Untrusted journal content must be text, never HTML");
class Node {
  constructor(tag="div"){this.tag=tag;this.children=[];this.style={};this.attributes={};this.textContent="";this.value="";this.disabled=false;this.listeners={}}
  append(...children){this.children.push(...children)}
  replaceChildren(...children){this.children=[...children]}
  setAttribute(key,value){this.attributes[key]=value}
  addEventListener(name,callback){this.listeners[name]=callback}
}
const stamp="2026-11-13T07:00:00+00:00";
const snap={slot_utc:stamp,collected_utc:stamp,book_time_utc:stamp,last_trade_utc:stamp,
  quality_status:"valid",quality_reason:"ok",last_price:"300",best_bid:"299.9",best_ask:"300.1",
  spread_pct:.0667,bids_total:100,asks_total:120};
const market={generated_utc:stamp,summary:[{symbol:"SBER",board:"TQBR",measurements:1,last:snap,statuses:{valid:1},median_valid_spread_pct:.0667}],
  history:{"SBER@TQBR":{symbol:"SBER",board:"TQBR",data:[snap]}},anomalies:[]};
const system={version:"1.11.0",generated_utc:stamp,budget:{calls:24,max_calls:24,actual_cost_usd:null},
  portfolio:{equity_rub:"100000",cash_rub:"90000",utc:stamp},market:{configured:1,healthy:1},
  services:[{service:"supervisor",status:"ok",recent:true}],openai_block:{blocked:true,reason:"insufficient_quota"},
  recent_decisions:[{utc:stamp,status:"hold",validation:"HOLD",decision_json:JSON.stringify({action:"HOLD",symbol:"NONE",reason:"Наблюдение без сделки"})}],
  orders:[],recent_events:[{utc:stamp,kind:"risk_alert",details_json:"<script>untrusted</script>"}]};
async function app(mode="normal"){
  const nodes=Object.fromEntries(ids.map(id=>[id,new Node()]));
  nodes.range.value="24";nodes.lots.value="10";nodes["anomaly-filter"].value="all";nodes["anomaly-instrument"].value="all";
  const listeners={},requests=[];
  const document={getElementById:id=>{assert(nodes[id],"Unknown DOM target: "+id);return nodes[id]},
    createElement:tag=>new Node(tag),createElementNS:(_,tag)=>new Node(tag),
    addEventListener:(name,cb)=>listeners[name]=cb};
  const context=vm.createContext({document,Intl,Date,console,setInterval:()=>0,
    fetch:async url=>{
      requests.push(url);assert(url.startsWith("/api/"),"External request forbidden");
      if(mode==="error")throw Error("fixture failure");
      let value;
      if(url.startsWith("/api/system"))value=system;
      else if(url.startsWith("/api/market"))value=mode==="empty"?{...market,history:{},summary:[]}:market;
      else value={snapshot:snap,levels:{bid:[{price:"299.9",lots:100}],ask:[{price:"300.1",lots:100}]},series:[],simulation:{},executable_quality:true};
      return {ok:true,json:async()=>value};
    }});
  vm.runInContext(code,context,{filename:"dashboard.js"});listeners.DOMContentLoaded();
  for(let i=0;i<4;i++)await new Promise(setImmediate);
  return {nodes,requests,context};
}
(async()=>{
  const normal=await app();
  assert.equal(normal.nodes["system-calls"].textContent,"24 / 24");
  assert(normal.nodes["system-block"].textContent.includes("заблокирован"));
  assert.equal(normal.nodes["decision-journal"].children.length,1);
  assert(normal.nodes["price-chart"].children.length>0);
  assert.equal(normal.nodes.refresh.disabled,false);
  assert.equal(normal.requests.length,3);
  normal.context.drawChart("price-chart",[snap,{...snap,slot_utc:"2026-11-14T07:00:00+00:00"}],
    [{key:"last_price",color:"blue"}],{});
  const paths=normal.nodes["price-chart"].children[0].children.filter(x=>x.tag==="path");
  assert.equal(paths.length,2,"Missing intervals must not be bridged by a price line");
  const empty=await app("empty");
  assert(empty.nodes.error.textContent.includes("нет инструментов"));
  assert.equal(empty.nodes["system-calls"].textContent,"24 / 24");
  const failed=await app("error");
  assert(failed.nodes.conn.textContent.includes("Ошибка"));
  assert(failed.nodes["system-stamp"].textContent.includes("недоступно"));
  assert.equal(failed.nodes.refresh.disabled,false);
  console.log("Dashboard DOM contracts: normal, empty, outage and gap rendering passed");
})().catch(error=>{console.error(error);process.exitCode=1});
