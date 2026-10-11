// jobs.js — 任务视图：进度条、步骤条、日志流（SSE）
//
// 同一个组件被两处复用：全局「任务」抽屉，和项目工作台里内嵌的执行面板。
// 所以它不自己找 DOM，而是 mount 到一个元素上。
import { $, esc, fmtEta, fmtClock, statusClass, statusText, isTerminal } from './util.js';

// 日志是**追加**的，一个跑了半天的任务能吐几千行。超了从头砍，
// 不是从尾砍 —— 尾部是用户正在看的最新进度。
export const MAX_LOG_NODES = 2000;

// 步骤有两个字段，**必须合起来用**：`steps` 是 {key: 状态} 字典（只含已轮到的），
// `step_specs` 才是有序清单。只拿 `steps` 不仅顺序不保证，还没跑到的步骤直接消失；
// 直接对 `steps` 调 .map 会报 "is not a function" 并把整个工作台渲染打断
// —— 症状是任务跑完了，⑤ 成片区却不出现播放器。
function stepList(snap) {
  const byKey = snap.steps || {};
  if (Array.isArray(byKey)) return byKey;          // 老形状，容忍
  const specs = snap.step_specs;
  if (Array.isArray(specs) && specs.length) {
    return specs.map((sp) => ({ ...sp, ...(byKey[sp.key] || {}) }));
  }
  return Object.values(byKey);
}

export function mountJobView(root) {
  root.innerHTML = `
    <div class="jv-head">
      <span class="jv-title muted small"></span>
      <span class="jv-status badge"></span>
    </div>
    <div class="progress-wrap">
      <div class="progress"><div class="progress-bar"></div></div>
      <div class="progress-meta">
        <span class="jv-prog small"></span>
        <span class="jv-eta muted small"></span>
      </div>
    </div>
    <ol class="steps"></ol>
    <div class="warn hidden"></div>
    <div class="logbox"></div>`;

  // 没有任务时整段收起来。否则空进度条和空日志框会一直占着一块黑，
  // 看着像「跑坏了」，其实只是还没开始。
  root.classList.add('idle');

  const el = {
    title: $('.jv-title', root),
    status: $('.jv-status', root),
    bar: $('.progress-bar', root),
    prog: $('.jv-prog', root),
    eta: $('.jv-eta', root),
    steps: $('.steps', root),
    warn: $('.warn', root),
    log: $('.logbox', root),
  };

  let jobId = null;
  let progress = 0;

  function setJob(snap) {
    root.classList.toggle('idle', !snap);
    if (!snap) {
      jobId = null;
      el.title.textContent = '';
      el.status.className = 'jv-status badge';
      el.status.textContent = '';
      el.bar.style.width = '0%';
      el.prog.textContent = '';
      el.eta.textContent = '';
      el.steps.innerHTML = '';
      el.log.innerHTML = '';
      el.warn.classList.add('hidden');
      return;
    }
    jobId = snap.id;
    el.title.textContent = snap.id || '';
    el.status.className = `jv-status badge ${statusClass(snap.status)}`;
    el.status.textContent = statusText(snap.status);

    if (typeof snap.progress === 'number') setProgress(snap.progress, snap.phase);
    if (snap.status === 'done') { setProgress(1, '完成'); el.eta.textContent = ''; }
    if (snap.status === 'failed' && snap.error) {
      el.prog.textContent = `失败：${snap.error}`;
    }

    el.steps.innerHTML = stepList(snap).map((s) => `
      <li class="${statusClass(s.status)}">
        <span class="st-name">${esc(s.label || s.key)}</span>
        <span class="st-status">${esc(statusText(s.status))}</span>
        <span class="muted small">${esc(fmtEta(s.elapsed))}</span>
      </li>`).join('');

    const warns = snap.warnings || [];
    el.warn.classList.toggle('hidden', !warns.length);
    el.warn.innerHTML = warns.map((w) => `<div>⚠️ ${esc(w)}</div>`).join('');

    if (snap.logs && snap.logs.length) {
      el.log.innerHTML = '';
      appendLog(snap.logs.map((l) => (typeof l === 'string' ? l : l.line)));
      scrollLog(true);
    }
  }

  function setProgress(v, phase) {
    progress = Math.max(0, Math.min(1, Number(v) || 0));
    el.bar.style.width = `${(progress * 100).toFixed(1)}%`;
    el.prog.textContent = `${(progress * 100).toFixed(1)}%`
      + (phase ? `  ${phase}` : '');
  }

  function appendLog(lines) {
    if (!lines.length) return;
    const near = el.log.scrollTop + el.log.clientHeight >= el.log.scrollHeight - 40;
    const frag = document.createDocumentFragment();
    for (const line of lines) {
      const div = document.createElement('div');
      div.className = 'logline';
      div.textContent = line == null ? '' : String(line);
      frag.appendChild(div);
    }
    el.log.appendChild(frag);
    while (el.log.childNodes.length > MAX_LOG_NODES) {
      el.log.removeChild(el.log.firstChild);
    }
    if (near) scrollLog();
  }

  function scrollLog(force) {
    if (force || el.log.scrollTop + el.log.clientHeight >= el.log.scrollHeight - 80) {
      el.log.scrollTop = el.log.scrollHeight;
    }
  }

  // 返回给调用方：SSE 事件直接喂进来
  function onEvent(ev) {
    if (ev.type === 'log') {
      const d = ev.data;
      appendLog([typeof d === 'string' ? d : (d && d.line)]);
    } else if (ev.type === 'progress') {
      setProgress(ev.data && ev.data.progress, ev.data && ev.data.phase);
    } else if (ev.type === 'state' || ev.type === 'warning') {
      setJob(ev.data);
    }
  }

  return { setJob, onEvent, appendLog, get jobId() { return jobId; }, get el() { return el; } };
}

// ------------------------------------------------------------------ SSE

// EventSource 自己会重连，并带上 Last-Event-ID；服务端按它补发漏掉的日志。
// 只在「任务还在跑」时订阅 —— 终态任务不需要长连接。
export function subscribe(jobId, onEvent, onDone) {
  let es = null;
  let closed = false;

  function open() {
    es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    for (const type of ['log', 'progress', 'state', 'warning', 'message']) {
      es.addEventListener(type, (e) => {
        let data = null;
        try { data = JSON.parse(e.data); } catch { data = e.data; }
        onEvent({ type, data });
        if (type === 'state' && data && isTerminal(data.status) && onDone) onDone(data);
      });
    }
    es.onerror = () => {
      // 服务端主动断流（任务结束）时也会走到这里；别弹错误，等它自然重连
      if (closed) es.close();
    };
  }

  open();
  return () => { closed = true; if (es) es.close(); };
}

// ------------------------------------------------------------------ 全局任务列表 + 抽屉

export function renderJobCards(box, jobs, onOpen) {
  if (!jobs.length) {
    box.innerHTML = '<div class="empty">还没有任务。进项目里点「开始执行」提交一个。</div>';
    return;
  }
  box.innerHTML = jobs.map((j) => `
    <div class="card job-card" data-job="${esc(j.id)}">
      <div class="card-head">
        <span class="card-title">${esc(j.project || j.id)}</span>
        <span class="badge ${statusClass(j.status)}">${esc(statusText(j.status))}</span>
      </div>
      <div class="muted small">${esc(j.category || '')} · ${esc(j.pipeline || '')}
        · ${esc(fmtClock(j.created_at))}</div>
      <div class="progress mini"><div class="progress-bar"
        style="width:${((Number(j.progress) || 0) * 100).toFixed(1)}%"></div></div>
      ${j.error ? `<div class="warn inline">${esc(j.error)}</div>` : ''}
    </div>`).join('');

  box.querySelectorAll('.job-card').forEach((c) => {
    c.addEventListener('click', () => onOpen(c.dataset.job));
  });
}
