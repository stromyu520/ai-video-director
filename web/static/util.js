// util.js — 无依赖小工具。零构建，浏览器原生 ES module。
//
// 纪律：**任何插进 innerHTML 的字符串都必须过 esc()**。这里的输入全是
// 用户可控的（项目名是中文长句、日志来自脚本 stdout、yaml 是 LLM 写的），
// 漏一处就是一个注入口子。

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

// 项目名/分类名是中文，直接塞进 URL 会让 uvicorn 回
// 「Invalid HTTP request received」—— 一律百分号编码。
export function pseg(s) {
  return encodeURIComponent(String(s ?? ''));
}

export function pdir(category, name) {
  return `/api/projects/${pseg(category)}/${pseg(name)}`;
}

export function mediaUrl(category, name, file) {
  return `/media/${pseg(category)}/${pseg(name)}/${pseg(file)}`;
}

export function fmtSize(n) {
  const v0 = Number(n) || 0;
  if (v0 < 1024) return `${v0} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = v0 / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v >= 10 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export function fmtDur(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

export function fmtAge(sec) {
  const s = Number(sec) || 0;
  if (s < 60) return '刚刚';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return new Date(Date.now() - s * 1000).toLocaleDateString('zh-CN');
}

export function fmtClock(t) {
  const d = new Date((Number(t) || 0) * 1000);
  if (!Number(t)) return '';
  return d.toLocaleString('zh-CN', { hour12: false });
}

// 状态 → 中文 + 颜色类。后端的状态名见 web/models.py。
export const STATUS_TEXT = {
  queued: '排队中', running: '进行中', waiting: '等待确认',
  done: '完成', failed: '失败', cancelled: '已取消', interrupted: '已中断',
  pending: '待执行', skipped: '已跳过',
};

export function statusText(s) {
  return STATUS_TEXT[s] || s || '';
}

export function statusClass(s) {
  if (s === 'done') return 'ok';
  if (s === 'failed' || s === 'interrupted') return 'bad';
  if (s === 'running' || s === 'waiting') return 'busy';
  if (s === 'cancelled' || s === 'skipped') return 'mute';
  return '';
}

export function isTerminal(s) {
  return ['done', 'failed', 'cancelled', 'interrupted'].includes(s);
}

// ---------------------------------------------------------------- 网络

export async function api(path, opts = {}) {
  const { body, headers, ...rest } = opts;
  const init = { ...rest, headers: { ...(headers || {}) } };
  if (body !== undefined) {
    init.method = init.method || 'POST';
    init.headers['Content-Type'] = 'application/json';
    init.body = typeof body === 'string' ? body : JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, init);
  } catch (e) {
    throw new Error(`连不上服务（${e.message}）`);
  }
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}

// ---------------------------------------------------------------- 提示

let toastTimer = null;

export function toast(msg, kind = '') {
  const box = $('#toast');
  if (!box) return;
  box.textContent = String(msg);
  box.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.className = 'toast hidden'; },
    kind === 'bad' ? 6000 : 3000);
}

export function fail(e) {
  toast(e && e.message ? e.message : String(e), 'bad');
}

// ---------------------------------------------------------------- DOM

export function show(node, on = true) {
  if (node) node.classList.toggle('hidden', !on);
}

// 顶层视图切换走的是 .view.active（style.css：.view{display:none}/.view.active{display:block}），
// 不是 .hidden。混用的话视图永远 display:none —— DOM 全渲染好了、卡片数也对，
// 但屏幕上什么也没有，最难查的一种「白屏」。
export function showView(node, on = true) {
  if (node) node.classList.toggle('active', !!on);
}

// 两种写法都收：on('#id', 'click', fn) 和 on(rootEl, '#id', 'click', fn)。
// 必须兼容两种 —— 少传 root 时形参整体前移，若不识别就会把选择器当元素用，
// 报「root.querySelector is not a function」并让整个 init() 半路死掉（页面白屏）。
export function on(root, sel, ev, fn) {
  if (fn === undefined) { [fn, ev, sel, root] = [ev, sel, root, document]; }
  const el = $(sel, root);
  if (el) el.addEventListener(ev, fn);
  return el;
}

// 把秒数说成人话（任务耗时、ETA）
export function fmtEta(sec) {
  const s = Number(sec) || 0;
  if (s <= 0) return '';
  if (s < 60) return `${Math.round(s)}s`;
  return `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, '0')}s`;
}
