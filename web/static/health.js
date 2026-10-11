// health.js — 顶栏那颗状态灯
import { $, esc, api } from './util.js';

let timer = null;
let last = null;                 // 最近一次 /api/health 的结果

// 别处要用健康状态时读这个（例如素材面板要提示「Pexels 没配」）。
// 返回 null = 还没拿到，调用方得当成「不知道」，不能当成「没配」。
export function snapshot() { return last; }

export function start(intervalMs = 20000) {
  tick();
  timer = setInterval(tick, intervalMs);
}

export function stop() {
  clearInterval(timer);
}

async function tick() {
  const dot = $('#healthDot');
  const txt = $('#healthText');
  if (!dot || !txt) return;
  try {
    const h = await api('/api/health');
    last = h;
    // 让已经画好的界面能补一次：健康状态是异步来的，冷启动直接进工作台
    // 时素材面板会先画完，「Pexels 未配」那类提示就再也补不上了。
    document.dispatchEvent(new Event('health'));
    const problems = [];
    if (!h.llm_ready) problems.push('LLM 未配');
    if (!h.ffmpeg) problems.push('ffmpeg 缺失');
    if (!h.pexels_ready) problems.push('Pexels 未配');

    dot.className = `dot ${problems.length ? 'dot-warn' : 'dot-ok'}`;
    const bits = [];
    if (h.gpu) bits.push(esc(h.gpu.split('\n')[0]));
    if (h.running) bits.push(`${h.running} 个任务在跑`);
    txt.innerHTML = problems.length
      ? `<span class="warn-text">${esc(problems.join(' · '))}</span>`
      : esc(bits.join(' · ') || '就绪');
    dot.title = [
      `python ${h.python}`, h.ffmpeg ? 'ffmpeg 可用' : 'ffmpeg 缺失',
      `LLM ${h.llm_ready ? '已配置' : '未配置'}`,
      `Pexels ${h.pexels_ready ? '已配置' : '未配置'}`,
    ].join('\n');
  } catch (e) {
    dot.className = 'dot dot-bad';
    txt.textContent = '服务未连接';
  }
}
