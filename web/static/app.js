// app.js — 路由 + 顶栏 + 配置弹窗 + 全局任务抽屉
//
// 路由用 hash（#/p/<分类>/<项目名>）：刷新留在原地、浏览器后退可用，
// 而且不需要服务端为每条路径都配一遍回退 —— 一个静态页就够了。
import { $, $$, esc, api, toast, fail, show, on, fmtClock, isTerminal,
         statusClass, statusText, pseg, showView } from './util.js';
import * as health from './health.js';
import * as projects from './projects.js';
import * as workspace from './workspace.js';
import { mountJobView, subscribe, renderJobCards } from './jobs.js';

let cats = ['新闻', '其他', '二创'];
let kinds = [];
let drawerView = null;
let drawerUnsub = null;
let route = { view: 'projects' };

// ------------------------------------------------------------------ 启动

async function init() {
  health.start();

  try {
    const k = await api('/api/kinds');
    kinds = k.kinds || [];
    cats = k.categories || cats;
  } catch { /* 起不来就先用内置默认值把界面画出来 */ }

  projects.setMeta(cats, kinds);
  projects.bindNewProject((p) => {
    location.hash = `#/p/${pseg(p.category)}/${pseg(p.name)}`;
  });

  $$('#tabs .tab').forEach((b) => b.addEventListener('click', () => {
    location.hash = b.dataset.view === 'jobs' ? '#/jobs' : '#/';
  }));
  on('#btnRefreshProjects', 'click', () => projects.load($('#filterCategory').value));
  on('#filterCategory', 'change', () => projects.load($('#filterCategory').value));
  on('#btnRefreshJobs', 'click', loadJobs);

  bindConfigDialog();
  bindDrawer();

  window.addEventListener('hashchange', routeTo);
  routeTo();
}

// ------------------------------------------------------------------ 路由

function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  if (!h) return { view: 'projects' };
  if (h === 'jobs') return { view: 'jobs' };
  const m = h.match(/^p\/([^/]+)\/([^/]+)$/);
  if (m) {
    try {
      return { view: 'workspace',
               category: decodeURIComponent(m[1]),
               name: decodeURIComponent(m[2]) };
    } catch { return { view: 'projects' }; }
  }
  return { view: 'projects' };
}

async function routeTo() {
  const r = parseHash();
  const changed = r.view !== route.view
    || r.category !== route.category || r.name !== route.name;
  route = r;

  // 离开工作台必须退订 SSE，否则切来切去会攒下一堆长连接
  if (changed) workspace.close();

  showView($('#view-projects'), r.view === 'projects');
  showView($('#view-jobs'), r.view === 'jobs');
  showView($('#view-workspace'), r.view === 'workspace');
  $$('#tabs .tab').forEach((b) => b.classList.toggle(
    'active', b.dataset.view === (r.view === 'jobs' ? 'jobs' : 'projects')));

  if (r.view === 'projects') await projects.load($('#filterCategory').value);
  else if (r.view === 'jobs') await loadJobs();
  else if (changed || !workspace.isOpen?.()) await workspace.open(r.category, r.name);
}

// ------------------------------------------------------------------ 任务视图

async function loadJobs() {
  let data;
  try { data = await api('/api/jobs?limit=50'); } catch (e) { fail(e); return; }
  const jobs = data.jobs || [];
  const badge = $('#jobBadge');
  const live = jobs.filter((j) => !isTerminal(j.status)).length;
  if (badge) {
    show(badge, live > 0);
    badge.textContent = live;
  }
  renderJobCards($('#jobList'), jobs, openDrawer);
}

function bindDrawer() {
  drawerView = mountJobView($('#drawerBody'));
  on('#btnCloseDrawer', 'click', closeDrawer);
  on('#btnCancel', 'click', async () => {
    if (!drawerView.jobId) return;
    try {
      await api(`/api/jobs/${encodeURIComponent(drawerView.jobId)}/cancel`, { body: {} });
      toast('已取消');
    } catch (e) { fail(e); }
  });
}

async function openDrawer(jobId) {
  const dlg = $('#drawer');
  let snap;
  try { snap = await api(`/api/jobs/${encodeURIComponent(jobId)}`); }
  catch (e) { fail(e); return; }

  $('#drawerTitle').textContent = snap.project || snap.id;
  $('#drawerSub').textContent =
    `${snap.category || ''} · ${snap.pipeline || ''} · ${fmtClock(snap.created_at)}`;
  const cancel = $('#btnCancel');
  cancel.classList.toggle('hidden', isTerminal(snap.status));

  drawerView.setJob(snap);
  dlg.classList.remove('hidden');

  // 详情接口和 SSE 首帧之间会漏日志 —— 服务端在订阅时会重放 history 并补一份
  // state 快照（见 server.job_events），所以这里什么都不用做。
  if (drawerUnsub) { drawerUnsub(); drawerUnsub = null; }
  if (!isTerminal(snap.status)) {
    drawerUnsub = subscribe(jobId, (ev) => drawerView.onEvent(ev), async (s) => {
      drawerView.setJob(s);
      cancel.classList.add('hidden');
      if (drawerUnsub) { drawerUnsub(); drawerUnsub = null; }
      await loadJobs();
    });
  }
}

function closeDrawer() {
  if (drawerUnsub) { drawerUnsub(); drawerUnsub = null; }
  $('#drawer').classList.add('hidden');
}

// ------------------------------------------------------------------ 配置

async function bindConfigDialog() {
  const dlg = $('#configDlg');
  on('#btnConfig', 'click', async () => {
    await paintConfig();
    dlg.showModal();
  });
  on('#btnSaveCfg', 'click', async (e) => {
    e.preventDefault();
    const body = {
      // 不带 profile：请求参数档按 model/base_url 推导，不是用户填的
      llm: { base_url: v('#cfgUrl'), model: v('#cfgModel'), api_key: v('#cfgKey') },
      pexels: { api_key: v('#cfgPexels') },
      vlm: {
        enabled: $('#cfgVlmOn').checked,
        // 空 = 不改 —— 和 key 一样的规矩，见 web/config.py save()。
        // 这里不是密钥，但空串同样会写成空目录名，必须挡掉。
        ...(v('#cfgVlmDir') ? { model_dir: v('#cfgVlmDir') } : {}),
      },
      defaults: {
        express: $('#cfgExpress').checked,
        auto_confirm_keywords: $('#cfgAutoKw').checked,
      },
      tts: {
        active: v('#cfgTtsActive'),
        // 只提交 edge：OpenAI 兼容那套已从页面撤掉，但配置项仍留在 config.toml 里
        // （要用手改 toml）。这里别再带上它 —— 页面上的空输入框会把
        // 手改好的 base_url 再用空串覆盖回去（key 有空串=不改的保护，base_url 没有）。
        backends: { edge: { voice: v('#cfgEdgeVoice') } },
      },
    };
    try {
      await api('/api/config', { body });
      toast('配置已保存');
      dlg.close();
    } catch (err) { fail(err); }
  });
  // 模型名一改，档就可能跟着变 —— 但新档要等服务端按新模型名算，所以这里
  // 只是把提示改成「保存后重新判断」，绝不自己在前端猜。
  on('#cfgModel', 'input', paintProfileHint);
  on('#btnTestLLM', 'click', async () => {
    const out = $('#cfgTestResult');
    out.textContent = '测试中…';
    out.className = 'small';
    try {
      const r = await api('/api/config/test-llm', { body: {} });
      out.textContent = `✅ ${r.model} 回了「${r.reply}」`;
      out.className = 'small ok-text';
    } catch (e) {
      out.textContent = `❌ ${e.message}`;
      out.className = 'small warn-text';
    }
  });
}

function v(sel) {
  const el = $(sel);
  return el ? el.value.trim() : '';
}

// 「请求参数档」原来是个下拉框，问的是「这家服务商吃哪套请求参数」——
// 而答案在模型名里已经写着，不该让人选。现在由服务端按 model/base_url 推导，
// 这里只把结果显示出来。
//
// 刻意**不在前端再抄一份**关键词表：抄一份就有两份，两份迟早不一致，
// 而不一致的时候页面说「k3 档」、管线却带了 reasoning_effort，
// 这种事只在对方回 400 的时候才暴露。判据的唯一出处是 pipeline_config.profile_for()。
const PROFILE_TEXT = {
  k3: '不带 reasoning_effort —— k3 只接受 temperature=1，带了会直接 400。',
  deepseek: '带 reasoning_effort=low —— 保留一点思考但别把 max_tokens 吃光。',
  openai: '带 reasoning_effort=low —— 对方不认这个参数的话，把模型名改成 k3 家的即可。',
};

// 打开弹窗时服务端给的「模型名 → 档」快照，用来判断模型是不是被改过
let profileBase = { model: '', name: '' };

function paintProfileHint() {
  const el = $('#cfgProfileHint');
  if (!el) return;
  const text = PROFILE_TEXT[profileBase.name] || '';
  const edited = v('#cfgModel') !== profileBase.model;
  el.textContent = `请求参数档（按模型自动判断）：${text}`
    + '模型名里带 k3 / kimi / moonshot 就自动换成不带额外参数的那档，不用手选。'
    + (edited ? ' —— 模型名改了，保存后按新模型重新判断。' : '');
}

async function paintConfig() {
  let c;
  try { c = await api('/api/config'); } catch (e) { fail(e); return; }
  $('#cfgPath').textContent = c.config_path || '';
  $('#cfgUrl').value = c.llm.base_url || '';
  $('#cfgModel').value = c.llm.model || '';
  profileBase = { model: c.llm.model || '', name: c.llm.profile || '' };
  paintProfileHint();
  $('#cfgKey').value = '';
  $('#cfgKeyHint').textContent = c.llm.api_key_set
    ? `已配置（${c.llm.api_key_masked}）。留空 = 不修改。`
    : '还没配 LLM key —— 生成脚本、二创编排都用不了。';
  $('#cfgPexels').value = '';
  $('#cfgPexelsHint').textContent = c.pexels.api_key_set
    ? `已配置（${c.pexels.api_key_masked}）。留空 = 不修改。`
    : '没配的话，画面写「搜索:xxx」的场景会降级成文字卡片（成片体积会明显变小）。';

  const vm = c.vlm || {};
  $('#cfgVlmOn').checked = vm.enabled !== false;
  $('#cfgVlmDir').value = vm.model_dir || '';
  $('#cfgVlmHint').textContent =
    '只对「手动上传的图片」跑（图片/ 目录），一张约 10s + 4.2GB 显存。'
    + '自动抽的视频帧、搜回来的素材都不喂给它。换模型只改这个目录。'
    + (vm.enabled === false ? '（当前关着）' : '');

  const t = c.tts || { backends: {} };
  const edge = t.backends.edge || {};
  // active 可能是手改 toml 才会出现的 'openai' —— 那个值在页面上没有对应选项，
  // 直接赋给 select 会把它清空。落到本地那栏，别把配置显示成「空」。
  const act = t.active || 'cosyvoice';
  $('#cfgTtsActive').value = act === 'openai' ? 'cosyvoice' : act;
  $('#cfgEdgeVoice').value = edge.voice || '';
}

document.addEventListener('DOMContentLoaded', init);
