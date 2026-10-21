// Adds click-for-a-plain-English-brief to an Archify HTML file.
// usage: node inject.mjs <base.html> <briefs.json> <out.html>
import { readFileSync, writeFileSync } from "node:fs";
const [base, briefsPath, out] = process.argv.slice(2);
const html = readFileSync(base, "utf8");
const briefs = JSON.parse(readFileSync(briefsPath, "utf8"));
if (!html.includes("</body>")) throw new Error("no </body> in base file");
const data = JSON.stringify(briefs).replace(/</g, "\\u003c");
const style = `<style id="hornet-brief-style">
#hornet-brief{position:fixed;right:16px;bottom:16px;width:min(380px,calc(100vw - 32px));max-height:min(70vh,560px);overflow:auto;
 background:var(--bg,#fff);color:var(--text,#111);border:1px solid var(--panel-border,#cbd5e1);border-radius:14px;padding:16px 18px;
 box-shadow:0 10px 30px rgba(0,0,0,.28);z-index:99999}
#hornet-brief,#hornet-brief *{font-family:"Segoe UI",Roboto,"Helvetica Neue",Arial,"Liberation Sans","DejaVu Sans",sans-serif !important}
#hornet-brief{font-size:14px;line-height:1.5}
#hornet-brief.hb-top{top:72px;bottom:auto}
#hornet-brief[hidden]{display:none}
#hornet-brief h2{margin:0 28px 2px 0;font-size:17px}
#hornet-brief .hb-tag{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-muted,#64748b);margin-bottom:8px}
#hornet-brief .hb-line{font-weight:600;margin:0 0 10px}
#hornet-brief dt{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-muted,#64748b);margin-top:10px}
#hornet-brief dd{margin:2px 0 0}
#hornet-brief button{position:absolute;top:8px;right:8px;border:0;background:transparent;color:inherit;font-size:20px;line-height:1;
 cursor:pointer;padding:4px 8px;border-radius:8px}
#hornet-brief button:hover,#hornet-brief button:focus-visible{background:rgba(148,163,184,.25);outline:none}
#hornet-brief-hint{position:fixed;left:16px;bottom:16px;z-index:99998;background:var(--bg,#fff);color:var(--text-muted,#64748b);
 border:1px solid var(--panel-border,#cbd5e1);border-radius:999px;padding:6px 12px;font:12px system-ui,sans-serif}
</style>`;
const script = `<script id="hornet-brief-script">(function(){
var B=${data};
var card=document.createElement("aside");card.id="hornet-brief";card.setAttribute("role","dialog");card.setAttribute("aria-label","Plain-English explanation");card.hidden=true;card.tabIndex=-1;
var close=document.createElement("button");close.type="button";close.setAttribute("aria-label","Close explanation");close.textContent="\\u00d7";
var h=document.createElement("h2"),tag=document.createElement("div"),line=document.createElement("p"),dl=document.createElement("dl");
tag.className="hb-tag";tag.textContent="In plain words";line.className="hb-line";
card.appendChild(close);card.appendChild(h);card.appendChild(tag);card.appendChild(line);card.appendChild(dl);
var hint=document.createElement("div");hint.id="hornet-brief-hint";hint.textContent="Click any box for a plain-English explanation";
function row(k,v){var dt=document.createElement("dt");dt.textContent=k;var dd=document.createElement("dd");dd.textContent=v;dl.appendChild(dt);dl.appendChild(dd)}
var last=null;
function open(id,el){var b=B[id];if(!b)return;last=el||null;h.textContent=b.title;line.textContent=b.line;dl.textContent="";
 row("What it does",b.does);row("Why it matters",b.matters);row("How it talks",b.talks);card.hidden=false;card.setAttribute("data-node",id);place(el);}
function place(el){card.classList.remove("hb-top");if(!el||!el.getBoundingClientRect)return;var a=el.getBoundingClientRect(),c=card.getBoundingClientRect();
 var hit=!(a.right<c.left||a.left>c.right||a.bottom<c.top||a.top>c.bottom);if(hit)card.classList.add("hb-top");}
function shut(){card.hidden=true;if(last&&last.focus)try{last.focus()}catch(e){}}
close.addEventListener("click",shut);
document.addEventListener("keydown",function(e){if(e.key==="Escape"&&!card.hidden){shut()}});
function wire(){document.body.appendChild(card);document.body.appendChild(hint);
 var nodes=document.querySelectorAll("[data-node-id]");
 for(var i=0;i<nodes.length;i++){(function(n){var id=n.getAttribute("data-node-id");if(!B[id])return;
  n.addEventListener("click",function(){open(id,n)});
  n.addEventListener("keydown",function(e){if(e.key==="Enter"||e.key===" "){open(id,n)}});})(nodes[i]);}
}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",wire);else wire();
})();</script>`;
const i = html.lastIndexOf("</body>");
writeFileSync(out, html.slice(0, i) + style + script + html.slice(i));
console.log("wrote", out, "with", Object.keys(briefs).length, "briefs");
