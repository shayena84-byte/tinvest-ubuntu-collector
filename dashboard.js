"use strict";

// No dependencies, no external requests. Market data come solely from local /api/market.
const $ = id => document.getElementById(id);
const svgNS = "http://www.w3.org/2000/svg";
let currentPayload = null;
let selectedKey = "";

const COLORS = {
  blue:"#66b1fa",teal:"#46dac2",amber:"#eab76f",
  red:"#f47e89",violet:"#b7a1fb",grid:"#2b3c51",muted:"#91a5bc"
};
const REASONS = {
  ok:"Подтверждено",
  crossed_or_locked_book:"Пересечение Bid ≥ Ask",
  orderbook_older_than_15m:"Стакан старше 15 минут",
  orderbook_timestamp_missing:"Нет времени стакана",
  orderbook_timestamp_invalid:"Неверное время стакана",
  orderbook_timestamp_in_future:"Время стакана из будущего",
  missing_book_side_or_price:"Пустая сторона стакана",
  historical_unverified:"Старая запись без проверки",
  not_checked:"Проверка не выполнялась"
};
const STATUSES = {
  valid:"Подтверждено",stale:"Устарело",
  unverified:"Не подтверждено",invalid:"Некорректно"
};
const df=new Intl.NumberFormat("ru-RU",{maximumFractionDigits:4});
function num(value,digits=2) {
  const n=Number(value);
  return value === null || value === undefined || !Number.isFinite(n)
    ? "—" : new Intl.NumberFormat("ru-RU",{maximumFractionDigits:digits}).format(n);
}
function stamp(iso) {
  if(!iso) return "время не указано";
  const date=new Date(iso);
  if(Number.isNaN(date.getTime())) return "время неизвестно";
  return new Intl.DateTimeFormat("ru-RU",{
    timeZone:"Europe/Moscow",day:"2-digit",month:"2-digit",
    hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false
  }).format(date);
}
function percent(value,d=3) {return value==null?"—":num(value,d)+"%";}
function node(tag,cls,text) {
  const n=document.createElement(tag);
  if(cls)n.className=cls;
  if(text!=null)n.textContent=String(text);
  return n;
}
function appendCell(row,text,cls) {
  const cell=node("td",cls||"",text);
  row.append(cell);
  return cell;
}
function statusTag(kind) {
  return node("span","status "+String(kind||"unverified"),STATUSES[kind]||"Неизвестно");
}
function svgNode(name,attributes,content) {
  const n=document.createElementNS(svgNS,name);
  for(const [key,val] of Object.entries(attributes||{}))n.setAttribute(key,String(val));
  if(content!=null)n.textContent=String(content);
  return n;
}
function drawChart(id,rows,series,opts) {
  const container=$(id);
  container.replaceChildren();
  const width=780,height=235,left=58,right=14,top=22,bottom=35;
  const data=[];
  for(let i=0;i<rows.length;i++) {
    const row=rows[i],ts=Date.parse(row.slot_utc);
    if(!Number.isFinite(ts))continue;
    const values=series.map(s=>{
      if(s.validOnly && row.quality_status!=="valid")return null;
      if(s.dropInvalid && row.quality_status==="invalid")return null;
      const v=Number(row[s.key]);
      if(row[s.key]==null || !Number.isFinite(v))return null;
      return v;
    });
    data.push({ts,values,quality:row.quality_status});
  }
  const points=data.flatMap(x=>x.values.filter(y=>y!==null));
  if(points.length<1) {
    container.append(node("div","empty",opts.empty||
      "Недостаточно подтверждённых данных. Продолжайте сбор наблюдений."));
    return;
  }
  let min=Math.min(...points),max=Math.max(...points);
  if(min===max){const delta=Math.max(Math.abs(max)*.008,.005);min-=delta;max+=delta}
  else {const delta=(max-min)*.10;min-=delta;max+=delta}
  const minT=Math.min(...data.map(p=>p.ts)),maxT=Math.max(...data.map(p=>p.ts));
  const scaleX=ts=>left+(maxT===minT ? 0.5 : (ts-minT)/(maxT-minT))*(width-left-right);
  const scaleY=v=>top+(max-v)/(max-min)*(height-top-bottom);
  const svg=svgNode("svg",{viewBox:"0 0 "+width+" "+height,
                           role:"img","aria-label":opts.label||"Временной график"});
  for(let i=0;i<=4;i++) {
    const y=top+(height-top-bottom)*i/4;
    svg.append(svgNode("line",{x1:left,y1:y,x2:width-right,y2:y,stroke:COLORS.grid,
                               "stroke-width":1,"stroke-dasharray":"3 5"}));
    const value=max-(max-min)*i/4;
    svg.append(svgNode("text",{x:left-10,y:y+4,fill:COLORS.muted,
                               "text-anchor":"end","font-size":"11"},num(value,opts.digits??2)));
  }
  if(data.length) {
    const first=new Date(minT),last=new Date(maxT);
    const label=ts=>new Intl.DateTimeFormat("ru-RU",{timeZone:"Europe/Moscow",
      day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}).format(new Date(ts));
    svg.append(svgNode("text",{x:left,y:height-9,fill:COLORS.muted,
      "font-size":"11"},label(first)));
    svg.append(svgNode("text",{x:width-right,y:height-9,fill:COLORS.muted,
      "font-size":"11","text-anchor":"end"},label(last)));
  }
  for(let si=0;si<series.length;si++) {
    const style=series[si];
    // Do not bridge gaps where qualified data were unavailable.
    let run=[];
    const flush=()=>{
      if(!run.length)return;
      const d=run.map((p,i)=>(i===0?"M":"L")+scaleX(p.ts).toFixed(2)+" "+scaleY(p.y).toFixed(2)).join(" ");
      svg.append(svgNode("path",{d,fill:"none",stroke:style.color,
                  "stroke-width":style.width||2.3,
                  "stroke-dasharray":style.dashed?"5 5":"none",
                  "stroke-linecap":"round","stroke-linejoin":"round",
                  opacity:style.opacity||1}));
      if(run.length===1){
        svg.append(svgNode("circle",{cx:scaleX(run[0].ts),cy:scaleY(run[0].y),
                   r:3,fill:style.color}));
      }
      run=[];
    };
    for(const p of data){
      const y=p.values[si];
      if(y===null){flush();continue}
      run.push({ts:p.ts,y});
    }
    flush();
  }
  container.append(svg);
}
function renderTable(rows) {
  const body=$("recent-body");
  body.replaceChildren();
  if(!rows.length){
    const tr=node("tr");appendCell(tr,"Нет записей за период");body.append(tr);return;
  }
  for(const r of [...rows].reverse().slice(0,12)){
    const tr=node("tr");
    appendCell(tr,stamp(r.collected_utc));
    appendCell(tr,num(r.last_price,4),"num");
    appendCell(tr,num(r.best_bid,4)+" / "+num(r.best_ask,4),"num");
    appendCell(tr,percent(r.spread_pct),"num");
    const td=node("td");
    td.append(statusTag(r.quality_status));
    td.title=REASONS[r.quality_reason]||r.quality_reason||"";
    tr.append(td);body.append(tr);
  }
}
function renderAnomalies(rows) {
  const body=$("anomaly-body");
  body.replaceChildren();
  if(!rows.length){
    const tr=node("tr");appendCell(tr,"За период аномалий не зафиксировано");body.append(tr);return;
  }
  for(const a of rows.slice(0,45)){
    const tr=node("tr");
    appendCell(tr,stamp(a.collected_utc));
    appendCell(tr,a.ticker+" / "+a.board);
    appendCell(tr,REASONS[a.reason]||a.reason);
    const td=node("td");td.append(statusTag(a.status));tr.append(td);
    body.append(tr);
  }
}
function render(){
  if(!currentPayload)return;
  const group=currentPayload.history[selectedKey];
  if(!group)return;
  const rows=group.data;
  const summary=currentPayload.summary.find(x=>x.symbol+"@"+x.board===selectedKey);
  const latest=summary?.last||null;
  const valid=summary?.statuses?.valid||0;
  const total=summary?.measurements||0;
  $("last-price").textContent=latest?num(latest.last_price,4)+(group.board==="TQCB"?"%":" ₽"):"—";
  $("last-stamp").textContent=latest&&latest.last_trade_utc?"Сделка: "+stamp(latest.last_trade_utc):"Время сделки не получено";
  $("bid-ask").textContent=latest?num(latest.best_bid,4)+" / "+num(latest.best_ask,4):"—";
  $("best-stamp").textContent=latest?"Стакан: "+stamp(latest.book_time_utc):"Стакан отсутствует";
  $("median-spread").textContent=percent(summary?.median_valid_spread_pct,4);
  $("usable").textContent=valid+" / "+total;
  $("usable-sub").textContent=total?num(100*valid/total,1)+"% пригодных":"Нет наблюдений";
  const lastQuality=latest?.quality_status||"unverified";
  const warning=$("quality-warning");
  if(lastQuality==="valid"){
    warning.textContent="Последний стакан подтверждён по времени и непротиворечив. Это не гарантирует фактического исполнения по показанной цене.";
    warning.style.borderLeftColor="var(--teal)";
  }else{
    warning.textContent="Последний стакан не пригоден для торгового исполнения: "+
      (REASONS[latest?.quality_reason]||latest?.quality_reason||"Нет данных")+
      ". Сырые наблюдения остаются в архиве, но исключены из подтверждённой статистики.";
    warning.style.borderLeftColor="var(--amber)";
  }
  for(const q of ["valid","stale","unverified","invalid"]){
    $("q-"+q).textContent=String(summary?.statuses?.[q]||0);
  }
  $("quality-sub").textContent="Из "+total+" замеров "+valid+" признаны пригодными";
  const validOnly=key=>({key,validOnly:true});
  drawChart("price-chart",rows,[
      {key:"last_price",color:COLORS.blue,dashed:true,opacity:.5},
      {key:"last_price",color:COLORS.teal,validOnly:true,width:2.8}
    ],{label:"Последняя цена: все наблюдения и подтвержденные",digits:3});
  drawChart("top-chart",rows,[
      {key:"best_bid",color:COLORS.teal,validOnly:true},
      {key:"best_ask",color:COLORS.amber,validOnly:true}
    ],{label:"Лучшие предложения спроса и продажи",digits:3});
  drawChart("spread-chart",rows,[
      {key:"spread_pct",color:COLORS.violet,validOnly:true}
    ],{label:"Относительный спред процентов",digits:3});
  drawChart("depth-chart",rows,[
      {key:"bids_total",color:COLORS.teal,validOnly:true},
      {key:"asks_total",color:COLORS.red,validOnly:true}
    ],{label:"Спрос и предложение в первых десяти уровнях",digits:0});
  renderTable(rows);
  renderAnomalies(currentPayload.anomalies||[]);
}
async function load(){
  const button=$("refresh"),badge=$("conn");
  button.disabled=true;
  badge.textContent="● Обновление…";
  $("error").style.display="none";
  try{
    const response=await fetch("/api/market?hours="+encodeURIComponent($("range").value),{
      cache:"no-store",headers:{"Accept":"application/json"}});
    if(!response.ok)throw new Error("HTTP "+response.status);
    const payload=await response.json();
    if(!payload||!payload.history)throw new Error("Некорректный ответ сервера");
    currentPayload=payload;
    const dropdown=$("instrument"),old=selectedKey;
    const keys=Object.keys(payload.history);
    dropdown.replaceChildren();
    for(const key of keys){
      const g=payload.history[key];
      const o=node("option","",g.symbol+"  •  "+g.board);
      o.value=key;dropdown.append(o);
    }
    selectedKey=keys.includes(old)?old:(keys[0]||"");
    dropdown.value=selectedKey;
    badge.textContent="● Соединение активно";
    badge.className="pill ok";
    $("updated").textContent="Обновлено: "+stamp(payload.generated_utc)+
      (payload.truncated?" · Часть истории ограничена":"")+
      " · UTC+3 (МСК)";
    if(selectedKey)render();
    else {$("error").textContent="В базе нет инструментов. Запустите сборщик."; $("error").style.display="block";}
  }catch(err){
    badge.textContent="● Ошибка данных";badge.className="pill err";
    $("error").textContent="Не удалось прочитать локальную SQLite-базу: "+
      String(err.message||err)+". Проверьте Market Recorder и журнал сервиса.";
    $("error").style.display="block";
  }finally{button.disabled=false}
}
document.addEventListener("DOMContentLoaded",()=>{
  $("range").addEventListener("change",load);
  $("instrument").addEventListener("change",event=>{
    selectedKey=event.target.value;render();
  });
  $("refresh").addEventListener("click",load);
  load();
  setInterval(load,60000);
});
