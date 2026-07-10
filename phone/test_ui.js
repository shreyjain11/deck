// Offline tests for the REAL client JS shipped inside server.py.
// Extracts the <script> body, shims a minimal DOM, evals it, and exercises
// md() (bug 3: safe markdown) and applyChat() (bug 1: incremental / dup-safe).
const fs = require('fs');
const src = fs.readFileSync(__dirname + '/server.py', 'utf8');
const m = src.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('no <script> found'); process.exit(2); }
let js = m[1];
// cut the DOM-bootstrapping tail (textarea listeners + subscribe()) so nothing
// touches EventSource/fetch at eval time; keep all function/var definitions.
const cut = js.indexOf("let T=document.getElementById('txt')");
if (cut > 0) js = js.slice(0, cut);

// ---- minimal DOM shim ----
function makeNode(tag) {
  return {
    tagName: tag, className: '', _t: '', _h: '', children: [], style: {},
    scrollTop: 0, clientHeight: 0, scrollHeight: 0,
    set textContent(v){ this._t = String(v); }, get textContent(){ return this._t; },
    set innerHTML(v){ if (v === '') this.children.length = 0; this._h = String(v); },
    get innerHTML(){ return this._h; },
    appendChild(c){ this.children.push(c); return c; },
    removeChild(c){ const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    classList: { _s:new Set(),
      add(x){this._s.add(x);}, remove(x){this._s.delete(x);},
      contains(x){return this._s.has(x);}, toggle(x,f){f?this._s.add(x):this._s.delete(x);} },
    querySelector(){ return makeNode('span'); },
  };
}
const chatEl = makeNode('div'), emptyEl = makeNode('div'), countEl = makeNode('div'), cardsEl = makeNode('div');
const byId = { chat: chatEl, empty: emptyEl, count: countEl, cards: cardsEl };
global.document = {
  getElementById: id => byId[id] || makeNode('div'),
  createElement: makeNode,
  querySelectorAll: () => [],
};
global.EventSource = function(){ return { close(){}, addEventListener(){} }; };
global.fetch = () => Promise.resolve({ json: () => ({}), text: () => '' });
global.location = { hostname: '127.0.0.1' };
global.setTimeout = () => {};
global.confirm = () => false;

// expose the closures we want to test
js += "\nglobalThis.__md=md;globalThis.__esc=esc;globalThis.__applyChat=applyChat;"
    + "globalThis.__setCUR=v=>{CUR=v};globalThis.__resetKeys=()=>{msgKeys=[]};"
    + "globalThis.__applyState=applyState;globalThis.__selectSession=selectSession;globalThis.__getCUR=()=>CUR;";
(0, eval)(js);
const { __md, __esc, __applyChat, __setCUR, __resetKeys } = globalThis;

let P = 0, Fl = 0;
const ok = (c, m) => { if (c) { P++; console.log('  PASS:', m); } else { Fl++; console.log('  FAIL:', m); } };

console.log('[markdown renderer (bug 3)]');
const out = __md("## Title\n\n**bold** and `inline`\n- a\n- b\n\n```js\nx<1 && y>2\n```");
ok(out.includes('<div class=mdh>Title</div>'), 'header -> mdh');
ok(out.includes('<b>bold</b>'), 'bold rendered');
ok(out.includes('<code>inline</code>'), 'inline code rendered');
ok(out.includes('<ul class=mdl>') && out.includes('<li>a</li>') && out.includes('<li>b</li>'), 'bullet list rendered');
ok(out.includes('<pre class=cb>') && out.includes('<code>'), 'fenced code block rendered');
ok(out.includes('x&lt;1 &amp;&amp; y&gt;2'), 'code contents HTML-escaped inside block');
const xss = __md('<img src=x onerror=alert(1)> **hi**');
ok(!xss.includes('<img') && xss.includes('&lt;img'), 'raw HTML is escaped, never injected');
ok(__esc('<b>&</b>') === '&lt;b&gt;&amp;&lt;/b&gt;', 'esc() escapes < > &');

console.log('[incremental chat render (bug 1)]');
__setCUR('work'); __resetKeys();
chatEl.children.length = 0;
// same message twice -> exactly two bubbles
__applyChat([{r:'user',t:'ping'},{r:'user',t:'ping'}]);
ok(chatEl.children.length === 2, 'identical message sent twice => exactly 2 bubbles');
const ref0 = chatEl.children[0], ref1 = chatEl.children[1];
// re-poll identical -> no rebuild (same node objects, same count)
__applyChat([{r:'user',t:'ping'},{r:'user',t:'ping'}]);
ok(chatEl.children.length === 2 && chatEl.children[0] === ref0 && chatEl.children[1] === ref1,
   'unchanged list => no re-render (same DOM nodes reused)');
// append one -> incremental append, existing nodes untouched
__applyChat([{r:'user',t:'ping'},{r:'user',t:'ping'},{r:'asst',t:'**reply**'}]);
ok(chatEl.children.length === 3 && chatEl.children[0] === ref0 && chatEl.children[1] === ref1,
   'appended message => only new bubble added (no full rebuild, no flicker)');
ok(chatEl.children[2]._h && chatEl.children[2]._h.includes('<b>reply</b>'),
   'assistant bubble uses markdown; new bubble content correct');
// divergence (window slid) -> full rebuild
__applyChat([{r:'asst',t:'different first'},{r:'user',t:'ping'}]);
ok(chatEl.children.length === 2 && chatEl.children[0] !== ref0, 'divergent list => clean rebuild');
// empty -> empty state
__applyChat([]);
ok(chatEl.children.length === 0 && emptyEl.style.display === 'flex', 'empty list => empty state shown');

console.log('[session targeting — the garble fix]');
const { __applyState, __selectSession, __getCUR } = globalThis;
__setCUR('');
const S2 = { name: 'B', status: 'idle', preview: 'y' }, S1 = { name: 'A', status: 'idle', preview: 'x' };
__applyState({ current: 'A', sessions: [S1, S2] });
ok(__getCUR() === 'A', 'unset -> CUR derives from server current');
__selectSession('B');
ok(__getCUR() === 'B', 'tapping B selects B');
__applyState({ current: 'A', sessions: [S1, S2] });   // routine tick, shared current still A
ok(__getCUR() === 'B', 'CUR stays B despite server current=A (no cross-session garble)');
__applyState({ current: 'A', sessions: [S1] });        // B vanished
ok(__getCUR() === 'A', 'CUR re-derives only when its session disappears');

console.log('\n=== js: ' + P + ' passed, ' + Fl + ' failed ===');
process.exit(Fl ? 1 : 0);
