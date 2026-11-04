// Run the actual UI initialization with an in-memory DOM and API, without a browser or uploads.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const profiles = JSON.parse(process.argv[2]);
const elements = new Map(), created = [];
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.value=""; this.listeners={}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  setAttribute() {}
  addEventListener(name, handler) { this.listeners[name]=handler; }
  closest() { return get("upload-label"); }
  before() {}
}
function get(id) {
  if(!elements.has(id)) { const el=new Element(id==="document-type"?"select":"div");el.id=id;elements.set(id,el); }
  return elements.get(id);
}
get("filter").value="all";
const responses={"/api/extraction-profiles":profiles,"/api/health":{provider_mode:"fixture"},
  "/api/documents":{documents:[]},"/api/evaluation":{}};
const context=vm.createContext({
  document:{getElementById:get,createElement:tag=>{const el=new Element(tag);created.push(el);return el;}},
  fetch:async(url,options)=>{assert.equal(options.method,"GET");assert.ok(url in responses);return {ok:true,json:async()=>responses[url]};},
  localStorage:{getItem:()=>null},location:{search:""},URLSearchParams,
});
let source=fs.readFileSync(path.join(__dirname,"../../dev/web/app.js"),"utf8");
source=source.replace(/initialize\(\)\.catch\(error=>\{[^\n]+\}\);\s*$/, "globalThis.uiInitialized=initialize();");
assert.ok(source.includes("globalThis.uiInitialized"));
vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web/results.js"),"utf8"),context);
vm.runInContext(source,context);
(async()=>{
  await context.uiInitialized;
  const upload=created.find(el=>el.id==="upload-document-type"),change=get("document-type");
  assert.ok(upload);
  for(const select of [upload,change]) {
    assert.deepEqual(select.children.map(option=>option.value),Object.keys(profiles));
    assert.ok(select.children.some(option=>option.value==="payslip_ocr"&&option.textContent==="급여명세서 · Python OCR"));
  }
  // Existing selection survives reloading the same profile list.
  change.value="payslip_ocr";
  vm.runInContext('populateDocumentTypes(document.getElementById("document-type"))',context);
  assert.equal(change.value,"payslip_ocr");
  assert.equal(change.children.length,Object.keys(profiles).length);
  console.log("Both upload and existing-document selectors expose all profiles, including Python OCR.");
})().catch(error=>{console.error(error);process.exitCode=1;});
