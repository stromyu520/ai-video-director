// workspace.js — 项目工作台：#/p/<分类>/<项目名>
//
// 用户的原话：「创建项目后应该进入里面，可以选择 tts（本地或线上），
// 素材自动搜索和上传素材，输出视频可以选择播放，选择视频属于哪种」。
// 所以这一页从上到下就是那几句话的顺序：
//   ① 做什么（类型） ② 配音 ③ 素材 ④ 执行 ⑤ 成片
//
// 一个纪律：这一页**不做任何长动作**，一律 POST 成任务交给后端跑 ——
// 浏览器关掉渲染照跑，SSE 断线能补发，这是 web/jobs.py 已经有的能力。
import {
  $, $$, esc, api, toast, fail, show, on, pseg, pdir, mediaUrl,
  fmtSize, fmtAge, fmtDur, fmtEta, statusClass, statusText, isTerminal, fmtClock,
} from './util.js';
import { mountJobView, subscribe } from './jobs.js';
import { snapshot as healthSnap } from './health.js';

// 健康状态回来得比工作台首帧晚，补一次重绘（见 health.js 里的派发点）。
// 只在确实开着工作台时才画，否则切到列表页还会被这里重画一次。
document.addEventListener('health', () => {
  if (S.state && S.state.project && $('#matBody')) paintMaterials();
});

const S = {
  category: '',
  name: '',
  state: null,
  kind: 'director',
  voices: null,
  voicesPromise: null,
  jobView: null,
  unsub: null,
  curJob: null,
  // 表单值在这里存一份，切类型/重渲染不丢
  form: {
    title: '', source_text: '', duration: 30, size: '9:16',
    preset: 'news',
    spk_id: '', express: true, backend: 'cosyvoice',
    auto_confirm_keywords: false,
    // 「自动搜索」开关。真值源是 project.json（服务端读盘），这里只是打开项目时
    // 抄一份、切开关时回写 —— 别在前端攒状态，否则换个浏览器进同一个项目，
    // 开关显示的是「开」，服务端跑的却是「关」。
    search: true,
  },
  source: { name: '', size: 0, width: 0, height: 0, duration: 0 },
};

export function currentRoute() {
  return { category: S.category, name: S.name };
}

// ------------------------------------------------------------------ 打开

export async function open(category, name) {
  S.category = category;
  S.name = name;
  S.state = null;
  S.curJob = null;
  if (S.unsub) { S.unsub(); S.unsub = null; }

  render(true);                              // 先出骨架，别让页面空着
  loadVoices();                              // 音色清单并行拉，不挡骨架

  let st;
  try {
    st = await api(`${pdir(category, name)}/state`);
  } catch (e) {
    fail(e);
    return;
  }
  S.state = st;
  // 建项目时选的类型优先；否则按目录内容推断的 suggest
  S.kind = st.project.kind || st.project.suggest || 'director';
  if (!S.form.title) S.form.title = st.project.name || '';
  // 开关以磁盘上的 project.json 为准（`!== false`：老项目没有这个键 → 默认开）
  S.form.search = st.project.search !== false;

  const src = st.project.source;
  if (src) {
    const f = (st.files || []).find((x) => x.name === src);
    S.source = { name: src, size: f ? f.size : st.project.source_size };
  }
  render(false);

  // 有在跑的任务就接着看它，否则显示最近一次的结果
  const live = (st.jobs || []).find((j) => !isTerminal(j.status));
  const last = live || (st.jobs || [])[0];
  if (last) bindJob(last);
}

function loadVoices() {
  if (S.voices || S.voicesPromise) return S.voicesPromise;
  S.voicesPromise = api('/api/voices')
    .then((v) => { S.voices = v; paintVoices(); return v; })
    .catch(() => { S.voicesPromise = null; });
  return S.voicesPromise;
}

export function close() {
  if (S.unsub) { S.unsub(); S.unsub = null; }
}

// ------------------------------------------------------------------ 渲染

const KIND_ORDER = ['director', 'commentary', 'remix'];

function render(skeleton) {
  const root = $('#workspace');
  if (skeleton && !S.state) {
    root.innerHTML = '<div class="empty">载入中…</div>';
    return;
  }
  const p = S.state.project;
  const kinds = S.state.kinds || [];

  root.innerHTML = `
    <div class="ws-head">
      <div>
        <div class="crumb muted small">${esc(p.category)} /</div>
        <h2 class="ws-title">${esc(p.name)}</h2>
        <div class="muted small">${esc(p.path)}</div>
      </div>
      <div class="ws-head-right">
        <span class="badge ok">${esc(fmtSize(p.output_size || 0))}</span>
        <button class="btn" id="wsReload">刷新</button>
      </div>
    </div>

    <section class="panel" id="sec-kind">
      <h3><span class="num">①</span> 做什么</h3>
      <div class="kinds" id="kindTabs">
        ${kinds.filter((k) => KIND_ORDER.includes(k.id)).map((k) => `
          <button class="kind ${k.id === S.kind ? 'active' : ''}" data-kind="${esc(k.id)}">
            <b>${esc(k.label)}</b><span class="muted small">${esc(k.hint)}</span>
          </button>`).join('')}
      </div>
      <div id="kindForm"></div>
    </section>

    <section class="panel" id="sec-voice">
      <h3><span class="num">②</span> 配音</h3>
      <div id="voiceBody"><div class="muted small">载入音色…</div></div>
    </section>

    <section class="panel" id="sec-mat">
      <h3><span class="num">③</span> 素材</h3>
      <div id="matBody"></div>
    </section>

    <section class="panel" id="sec-run">
      <h3><span class="num">④</span> 执行</h3>
      <div class="runbar" id="runBar"></div>
      <div id="jobView" class="jobview"></div>
    </section>

    <section class="panel" id="sec-out">
      <h3><span class="num">⑤</span> 成片</h3>
      <div id="outBody"></div>
    </section>`;

  S.jobView = mountJobView($('#jobView'));

  $$('#kindTabs .kind').forEach((b) => b.addEventListener('click', () => {
    setKind(b.dataset.kind);
  }));
  on(root, '#wsReload', 'click', () => open(S.category, S.name));

  paintKindForm();
  paintVoices();
  paintMaterials();
  paintRunBar();
  paintOutput();
  if (S.curJob) S.jobView.setJob(S.curJob);
}

function setKind(kind) {
  if (kind === S.kind) return;
  S.kind = kind;
  $$('#kindTabs .kind').forEach((b) => {
    b.classList.toggle('active', b.dataset.kind === kind);
  });
  // 类型落盘：下次进来直接展开对应那段，不用重新选
  api(`${pdir(S.category, S.name)}/kind`, { body: { kind } }).catch(fail);
  paintKindForm();
  paintRunBar();
  paintMaterials();
}

// ------------------------------------------------------------------ ① 做什么

// 「文案风格」以前叫「体裁」，光看名字没人知道它改的是什么 —— 实际上它只换
// 系统提示词，管线、素材、渲染、时长全都不受影响。所以除了改名，
// 选中哪条就把那条的写法写在下面。
const PRESET_HINT = {
  news: '新闻解说：从正文里挑核心事实、关键数据和引语，砍掉背景铺垫；'
      + '结构是 钩子 → 事实 → 争议 → 观点。',
  general: '通用口播：不套新闻的取舍规则，按主题自由展开；'
      + '结构是 钩子 → 展开 → 转折/高光 → 收尾。',
};


function paintPresetHint() {
  const el = $('#presetHint');
  if (el) {
    el.textContent = `${PRESET_HINT[S.form.preset] || ''}`
      + '（只决定智能体怎么写这份稿子，不影响管线与渲染。）';
  }
}


function paintKindForm() {
  const box = $('#kindForm');
  if (!box) return;
  const f = S.form;
  const isGen = S.kind === 'director';

  if (isGen) {
    box.innerHTML = `
      <div class="grid2">
        <label>标题 <input id="fTitle" type="text" value="${esc(f.title)}"
          placeholder="留空则取项目名"></label>
        <label>时长（秒） <input id="fDur" type="number" min="5" max="300"
          value="${esc(f.duration)}"></label>
      </div>
      <label>正文 <textarea id="fText" rows="6"
        placeholder="把新闻原文粘进来，智能体会据此写导演文件">${esc(f.source_text)}</textarea></label>
      <div class="grid2">
        <label>画面比例
          <select id="fSize">
            <option value="9:16"${f.size === '9:16' ? ' selected' : ''}>竖屏 9:16</option>
            <option value="16:9"${f.size === '16:9' ? ' selected' : ''}>横屏 16:9</option>
          </select></label>
        <label>文案风格
          <select id="fPreset">
            <option value="news"${f.preset === 'news' ? ' selected' : ''}>新闻解说</option>
            <option value="general"${f.preset === 'general' ? ' selected' : ''}>通用口播</option>
          </select></label>
      </div>
      <p class="muted small" id="presetHint"></p>
      <p class="muted small">正文 → 智能体编排 → director.yaml → 渲染成片。
        项目里已有 director.yaml 时，也可以只渲染不重写。</p>`;

    bindVal('#fTitle', 'title');
    bindVal('#fDur', 'duration', Number);
    bindVal('#fText', 'source_text');
    bindVal('#fSize', 'size');
    bindVal('#fPreset', 'preset');
    paintPresetHint();
    on(box, '#fPreset', 'change', paintPresetHint);
  } else {
    const label = S.kind === 'commentary' ? '视频解说' : '二次创作';
    const hint = S.kind === 'commentary'
      ? '保留原片部分原声，只给需要解说的镜头补旁白。'
      : '原片全程重配旁白与画面。';
    box.innerHTML = `
      <p class="muted small">${esc(label)}：${esc(hint)}</p>
      <div class="drop" id="dropZone">
        <input type="file" id="fileInput" accept="video/*" hidden>
        ${S.source.name
          ? `<div><b>${esc(S.source.name)}</b>
               <span class="muted small">${esc(fmtSize(S.source.size))}
               ${S.source.width ? ` · ${S.source.width}×${S.source.height}` : ''}
               ${S.source.duration ? ` · ${esc(fmtDur(S.source.duration))}` : ''}</span></div>
             <button class="btn" id="btnPick">换一个</button>`
          : `<div class="muted">把原片拖到这里，或 <button class="btn" id="btnPick">选择文件</button></div>`}
        <div class="upload hidden" id="upWrap">
          <div class="progress"><div class="progress-bar" id="upBar"></div></div>
          <div class="muted small" id="upText"></div>
        </div>
      </div>`;
    bindUpload();
  }
}

function bindVal(sel, key, cast) {
  const el = $(sel);
  if (!el) return;
  const ev = (el.type === 'checkbox' || el.tagName === 'SELECT') ? 'change' : 'input';
  el.addEventListener(ev, () => {
    let v = el.type === 'checkbox' ? el.checked : el.value;
    if (cast && typeof v !== 'boolean') v = cast(v);
    S.form[key] = v;
  });
}

// ------------------------------------------------------------------ ①b 上传

function bindUpload() {
  const zone = $('#dropZone');
  const input = $('#fileInput');
  if (!zone) return;
  on(zone, '#btnPick', 'click', (e) => { e.stopPropagation(); input.click(); });
  input.addEventListener('change', () => {
    if (input.files && input.files[0]) upload(input.files[0]);
  });
  ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => {
    e.preventDefault();
    zone.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach((ev) => zone.addEventListener(ev, (e) => {
    e.preventDefault();
    zone.classList.remove('over');
  }));
  zone.addEventListener('drop', (e) => {
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) upload(file);
  });
}

// 上传必须用 XHR：fetch 报不了上传进度（没有 upload.onprogress）。
// 200MB 的片子传到一半没反馈，用户会以为卡死。
function upload(file) {
  const wrap = $('#upWrap');
  const bar = $('#upBar');
  const txt = $('#upText');
  show(wrap, true);
  txt.textContent = `上传 ${file.name}（${fmtSize(file.size)}）…`;

  const fd = new FormData();
  fd.append('file', file, file.name);
  const xhr = new XMLHttpRequest();
  // 原片必须落根目录：_find_source_video 和 params.source 都按根目录找
  xhr.open('POST', `${pdir(S.category, S.name)}/upload?dir=`);
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = (e.loaded / e.total) * 100;
    bar.style.width = `${pct.toFixed(1)}%`;
    txt.textContent = `上传中 ${pct.toFixed(0)}% · ${fmtSize(e.loaded)} / ${fmtSize(e.total)}`;
  };
  xhr.onload = async () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      txt.textContent = '上传完成，正在探测时长与分辨率…';
      toast('原片已上传');
      await open(S.category, S.name);        // 重新拉 state，拿到实测尺寸
    } else {
      let msg = `上传失败 HTTP ${xhr.status}`;
      try { msg = JSON.parse(xhr.responseText).error || msg; } catch { /* 保持原样 */ }
      txt.textContent = msg;
      toast(msg, 'bad');
    }
  };
  xhr.onerror = () => { txt.textContent = '上传中断'; };
  xhr.send(fd);
}

// ------------------------------------------------------------------ ② 配音

function paintVoices() {
  const box = $('#voiceBody');
  if (!box) return;
  const v = S.voices;
  if (!v) { box.innerHTML = '<div class="muted small">载入音色…</div>'; return; }

  const backends = [{ id: 'cosyvoice', label: '本地 CosyVoice', ready: true,
                      enabled: true }].concat(v.backends || []);
  if (!S.form.spk_id) {
    const act = v.active || 'cosyvoice';
    const b = backends.find((x) => x.id === act && x.enabled && x.ready);
    S.form.backend = b ? b.id : 'cosyvoice';
    S.form.spk_id = b && b.id !== 'cosyvoice'
      ? (b.default_voice || '') : ((v.local[0] && v.local[0].id) || '');
  }

  const pool = listFor(S.form.backend);
  box.innerHTML = `
    <div class="radios">
      ${backends.filter((b) => b.enabled).map((b) => `
        <label class="radio ${b.ready ? '' : 'dim'}">
          <input type="radio" name="vbackend" value="${esc(b.id)}"
            ${b.id === S.form.backend ? 'checked' : ''} ${b.ready ? '' : 'disabled'}>
          <span>${esc(b.label)}</span>
          ${b.ready ? '' : '<span class="muted small">（未配置）</span>'}
        </label>`).join('')}
    </div>
    <div class="grid2">
      <label>音色 <select id="vVoice">${pool.map((o) => `
        <option value="${esc(o.id)}"${o.id === S.form.spk_id ? ' selected' : ''}>
          ${esc(o.label)}</option>`).join('')}</select></label>
      <label class="inline"><input id="vExpress" type="checkbox"
        ${S.form.express ? 'checked' : ''}> 情感表现（仅本地音色有效）</label>
    </div>
    <p class="muted small" id="vHint"></p>`;

  $$('input[name=vbackend]', box).forEach((r) => r.addEventListener('change', () => {
    S.form.backend = r.value;
    const p = listFor(r.value);
    // 用后端给的 default_voice，别拿 p[0] —— edge 的清单是按语言排的，
    // 第一条是「Adri (Natural) - Afrikaans」，中文项目默认配南非荷兰语音色。
    const b = backends.find((x) => x.id === r.value);
    const want = (b && b.default_voice) || '';
    const hit = want ? p.find((o) => o.id === want) : null;
    S.form.spk_id = ((hit || p[0]) || {}).id || '';
    paintVoices();
  }));
  on(box, '#vVoice', 'change', (e) => { S.form.spk_id = e.target.value; paintHint(); });
  on(box, '#vExpress', 'change', (e) => { S.form.express = e.target.checked; });
  paintHint();
}

function listFor(backend) {
  const v = S.voices;
  if (!v) return [];
  if (backend === 'cosyvoice') return v.local || [];
  return (v.online || []).filter((o) => o.provider === backend);
}

function paintHint() {
  const hint = $('#vHint');
  if (!hint) return;
  const online = S.form.spk_id.includes(':');
  const n = listFor(S.form.backend).length;
  hint.textContent = online
    ? '线上合成不加载本地模型（省 21s 与 3.2GB 显存），「情感表现」对线上无效。'
    : `本地音色共 ${n} 个；首次合成要加载模型，约 21s。`;
}

// ------------------------------------------------------------------ ③ 素材

function paintMaterials() {
  const box = $('#matBody');
  if (!box) return;
  const st = S.state;
  const scenes = st.scenes;

  // 只列真正的素材目录。以前这里还写着 '素材缓存' —— 那是**仓库根**下的
  // Pexels 缓存，list_files 只列项目内的条目，那个分支永远不会命中。
  const matDirs = (st.files || []).filter((f) => f.dir
    && ['素材', '图片', '短片'].includes(f.name));
  const h = healthSnap();
  // 关了就只排本地素材：智能体那边会禁掉 `搜索:`、场景数下限跟着素材走，
  // 渲染时也不会再发 Pexels 请求（见 web/pipelines.py 的 _no_search）。
  const search = S.form.search !== false;
  const nMat = matDirs.reduce((n, d) => n + (Number(d.count) || 0), 0);
  const head = `
    <div class="matbar">
      ${matDirs.map((d) => `<span class="chip">${esc(d.name)} · ${d.count}</span>`).join('')
        || '<span class="muted small">还没传过素材</span>'}
      <span class="spacer"></span>
      <label class="inline"><input id="matSearch" type="checkbox"
        ${search ? 'checked' : ''}> 自动搜索素材</label>
      <button class="btn" id="btnUploadMat">上传素材</button>
      <input type="file" id="matInput" multiple hidden>
    </div>
    <p class="muted small">
      素材有<b>两处来源，叠加使用，不是二选一</b>：
      <b>①你上传的</b>（图片和短视频都传 <code>素材/</code>，靠扩展名区分）会列给智能体，
      编排时优先用，讲哪一项就配印着那一项的那张图。
      ${search
        ? `<b>②它没覆盖到的场景</b>，自动按 <code>搜索:关键词</code> 去 Pexels 下载空镜。
           <b>一个都不传也照常出片</b> —— 整片都走 Pexels。`
        : `<b>②自动搜索已关</b>：缺的场面不再联网补空镜，
           改成<b>少排几场</b>、把旁白写长一点 —— 宁可片子短，也不拿文字卡凑。`}
    </p>
    ${search && h && h.pexels_ready === false ? `<p class="warn inline">
      ⚠️ 没配 Pexels API key：上面第 ② 类场景会降级成文字卡片，成片体积会明显变小
      （实测 28MB → 1.4MB）。到「配置 → 素材」填一个即可。</p>` : ''}`;

  if (!scenes || !scenes.items.length) {
    box.innerHTML = head + `
      <p class="muted small">还没有导演文件/二创方案，生成后这里会列出逐场景用哪些素材。</p>`;
    bindMatUpload();
    return;
  }

  // 逐场景点出来：哪些走本地、哪些走 Pexels。用户不必自己数表格。
  const isSearch = (s) => /^搜索\s*[:：]/.test((s || '').trim());
  const nSearch = scenes.items.filter((it) => isSearch(it.source)).length;
  const nLocal = scenes.items.filter((it) => it.source && !isSearch(it.source)).length;
  const nText = scenes.items.length - nSearch - nLocal;
  // 只有「搜索:」场景在没 key 时才会降级；type: text 是导演有意安排的收尾卡，
  // 标成红色会让人以为出错了。
  const pexelsOk = !h || h.pexels_ready !== false;   // 拿不到健康状态时不乱报
  const split = `
    <div class="matbar">
      ${nLocal ? `<span class="chip chip-ok">本地素材 · ${nLocal}</span>` : ''}
      ${nSearch ? `<span class="chip ${!search ? 'chip-bad'
        : (pexelsOk ? 'chip-warn' : 'chip-bad')}">
        ${search ? 'Pexels 自动搜' : '搜索: 但已关闭'} · ${nSearch}${
          search && !pexelsOk ? '（没配 key，会降级成文字卡片）' : ''}</span>` : ''}
      ${nText ? `<span class="chip">文字卡 · ${nText}</span>` : ''}
      <span class="muted small">这次会用的素材来源</span>
    </div>
    ${!search && nSearch ? `<p class="warn inline">
      自动搜索关着，但 <code>${esc(scenes.file)}</code> 里还有 ${nSearch} 个
      <code>搜索:</code> 场景 —— 渲染时它们不联网，会退到本地素材，顶不上就是文字卡。
      现在本地有 ${nMat} 件，大概够 ${nMat} 场。
      <b>重新生成一次导演文件</b>，智能体才会按「少排几场」重写这份稿子。</p>` : ''}`;

  // 逐场景秒数是按音色实测语速估的，加成总数 —— 片长由旁白决定，
  // 这个合计才是「时长」框真正会对上的那个数（渲染完再按实际值核对）。
  const total = Math.round(scenes.items.reduce(
    (n, it) => n + (Number(it.secs) || 0), 0) * 10) / 10;

  box.innerHTML = head + split + `
    <div class="muted small">来自 <code>${esc(scenes.file)}</code>
      · ${scenes.items.length} 个场景${scenes.error ? ` · ⚠️ ${esc(scenes.error)}` : ''}
      ${total ? `· 旁白合计约 ${total}s` : ''}</div>
    <table class="scenes">
      <thead><tr><th>#</th><th>画面</th><th>素材</th><th>旁白</th><th>时长</th></tr></thead>
      <tbody>${scenes.items.map((it) => `
        <tr>
          <td>${esc(it.index + 1)}</td>
          <td><span class="chip">${esc(it.kind || '—')}</span></td>
          <td class="src">${esc(it.source || '—')}</td>
          <td class="narr">${esc(it.narration || '（保留原声）')}</td>
          <td>${it.secs ? `${it.secs}s` : '—'}</td>
        </tr>`).join('')}</tbody>
    </table>`;
  bindMatUpload();
}

function bindMatUpload() {
  on($('#matBody'), '#btnUploadMat', 'click', () => $('#matInput').click());
  on($('#matBody'), '#matInput', 'change', async (e) => {
    const files = [...(e.target.files || [])];
    for (const f of files) await uploadSilent(f);
    toast(`已上传 ${files.length} 个素材`);
    await open(S.category, S.name);
  });
  on($('#matBody'), '#matSearch', 'change', (e) => setSearch(e.target.checked));
}

// 开关立刻落盘（project.json），不是只存在内存里 —— 换个页面/换台机器进来
// 看到的还是同一个值，而任务提交时读的也是它（见 buildRequest）。
// 回写失败就把开关拨回去：界面显示「关」、服务端跑「开」是这里最难查的一种错，
// 只在成片里表现成「怎么还是搜了素材」。
async function setSearch(on) {
  S.form.search = on;
  try {
    await api(`${pdir(S.category, S.name)}/settings`, { body: { search: on } });
  } catch (e) {
    S.form.search = !on;
    fail(e);
  }
  paintMaterials();
}

// 上传的素材一律落 `素材/`（图片和短视频同一层，靠扩展名区分）。
// 以前按扩展名分流到 `图片/` 和 `素材/` 两处，用户得记住「图在哪个文件夹」；
// 落错地方还是**死文件** —— 没有任何一步会发现它，而且一声不吭。
// 目录约定的唯一真值源是 pipeline_config.MANUAL_DIRS。
function uploadSilent(file, dir) {
  const target = dir !== undefined ? dir : '素材';
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${pdir(S.category, S.name)}/upload?dir=${encodeURIComponent(target)}`);
    xhr.onload = () => (xhr.status < 300 ? resolve() : reject(new Error(`HTTP ${xhr.status}`)));
    xhr.onerror = () => reject(new Error('上传中断'));
    xhr.send(fd);
  }).catch(fail);
}

// ------------------------------------------------------------------ ④ 执行

// writeScript=false → generate 步骤的 when 谓词判否，直接用现有 director.yaml 渲染
function buildRequest(writeScript = true) {
  const f = S.form;
  if (S.kind === 'director') {
    return {
      pipeline: 'director',
      params: {
        title: f.title, source_text: f.source_text,
        duration: Number(f.duration) || 30, size: f.size, preset: f.preset,
        write_script: !!writeScript,
        spk_id: f.spk_id, express: !!f.express,
        // 素材面板上的「自动搜索」开关。跟着任务走而不是只写进 project.json：
        // 「生成脚本」和「渲染成片」是两个独立任务，同一个开关值两边都要收到
        // （生成侧禁 搜索:，渲染侧不联网），少一边就会出现半开半关。
        search: !!f.search,
        auto_confirm_keywords: !!f.auto_confirm_keywords,
      },
    };
  }
  const mode = S.kind === 'commentary' ? 'smart' : 'full';
  const params = {
    mode, spk_id: f.spk_id, express: !!f.express,
    search: !!f.search,
    auto_confirm_keywords: !!f.auto_confirm_keywords,
  };
  if (S.source.name) params.source = `${S.state.project.path}/${S.source.name}`;
  return { pipeline: 'remix', params };
}

function paintRunBar() {
  const box = $('#runBar');
  if (!box) return;
  const busy = S.curJob && !isTerminal(S.curJob.status);
  const gen = S.kind === 'director';

  box.innerHTML = `
    <button class="btn btn-primary" id="btnRun" ${busy ? 'disabled' : ''}>
      ${gen ? '生成脚本并渲染' : '开始解说/二创'}
    </button>
    ${gen ? `<button class="btn" id="btnRender" ${busy ? 'disabled' : ''}>
       只渲染现有脚本</button>` : ''}
    <button class="btn btn-danger" id="wsCancel" ${busy ? '' : 'disabled'}>取消</button>
    <span class="muted small" id="runHint"></span>`;

  const hint = $('#runHint');
  if (hint) {
    hint.textContent = gen
      ? '智能体会先出 director.yaml，校验不过会自动重写（最多 3 次）。'
      : `原片：${S.source.name || '（还没上传）'}`;
  }

  on(box, '#btnRun', 'click', () => submit(buildRequest(true)));
  on(box, '#btnRender', 'click', () => submit(buildRequest(false)));
  // 注意别叫 btnCancel：抽屉里那个同名，document 上取会拿错元素
  on(box, '#wsCancel', 'click', async () => {
    if (!S.curJob) return;
    try {
      await api(`/api/jobs/${encodeURIComponent(S.curJob.id)}/cancel`, { body: {} });
      toast('已取消');
    } catch (e) { fail(e); }
  });
}

async function submit(req) {
  try {
    const job = await api(`${pdir(S.category, S.name)}/jobs`, { body: req });
    toast('任务已提交');
    bindJob(job);
  } catch (e) {
    fail(e);
  }
}

function bindJob(job) {
  S.curJob = job;
  if (!S.jobView) return;
  if (S.unsub) { S.unsub(); S.unsub = null; }
  S.jobView.setJob(job);
  paintRunBar();
  if (!isTerminal(job.status)) {
    S.unsub = subscribe(job.id, (ev) => S.jobView.onEvent(ev), async (snap) => {
      S.curJob = snap;
      if (S.unsub) { S.unsub(); S.unsub = null; }
      paintRunBar();
      // 跑完重新拉一次 state：成片、场景、文件都变了
      await refreshQuiet();
    });
  }
}

async function refreshQuiet() {
  try {
    const st = await api(`${pdir(S.category, S.name)}/state`);
    S.state = st;
    paintMaterials();
    paintOutput();
    paintRunBar();
    const src = st.project.source;
    if (src) {
      const f = (st.files || []).find((x) => x.name === src);
      S.source = { name: src, size: f ? f.size : 0 };
      if (S.kind !== 'director') paintKindForm();
    }
  } catch { /* 刷新失败不打扰用户，他还能手动点刷新 */ }
}

// ------------------------------------------------------------------ ⑤ 成片

function paintOutput() {
  const box = $('#outBody');
  if (!box) return;
  const p = S.state.project;
  const out = (p.artifacts || []).find((a) => a.name === p.output);
  if (!out) {
    box.innerHTML = '<p class="muted small">还没有成片。跑完第 ④ 步就会出现在这里。</p>';
    return;
  }
  box.innerHTML = `
    <video class="player" controls preload="metadata"
           src="${esc(mediaUrl(p.category, p.name, out.name))}"></video>
    <div class="muted small">${esc(out.name)} · ${esc(fmtSize(out.size))}
      · ${esc(fmtAge(out.age))} 前生成
      · <a href="${esc(out.url)}" download>下载</a></div>`;

  // 「意图 + 事后核对」的核对那半边：片长是旁白念出来的，不是「时长」框说了算。
  // 差值以前只进渲染日志，而日志是滚动的 —— 片子都出来了，没人会回头翻。
  const d = S.state.duration;
  if (d && d.off_budget) {
    const chars = ((S.state.scenes || {}).items || [])
      .reduce((n, s) => n + ((s.narration || '').length), 0);
    const aim = chars ? Math.max(1, Math.round(chars * d.want / d.actual)) : 0;
    box.insertAdjacentHTML('beforeend',
      `<div class="warn inline">⚠️ 你要 ${d.want}s，实际 ${d.actual}s`
      + `（${d.longer ? '长' : '短'}了 ${Math.round(Math.abs(d.off) * 100)}%）。`
      + ' 片长由旁白念多久决定，跟「时长」框填的数没有直接关系。'
      + (d.longer
          ? ` 这份稿子共 ${chars} 字，要出 ${d.want}s 得压到约 ${aim} 字。`
          : ' 要出更长的片，正文得写得更长。')
      + ' 改完正文点「生成脚本并渲染」重跑。</div>');
  } else if (d) {
    box.insertAdjacentHTML('beforeend',
      `<div class="muted small">片长 ${d.actual}s，与你要的 ${d.want}s 相符。</div>`);
  }

  const others = (p.artifacts || []).filter((a) => a.name !== out.name);
  if (others.length) {
    box.insertAdjacentHTML('beforeend', `
      <div class="muted small" style="margin-top:6px">产物：
        ${others.map((a) => `<a href="${esc(a.url)}" target="_blank"
          rel="noopener">${esc(a.label || a.name)}</a>`).join(' · ')}</div>`);
  }
}

// 供 app.js 判定当前是否在工作台
export function isOpen() {
  return !!S.category;
}
