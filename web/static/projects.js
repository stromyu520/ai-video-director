// projects.js — 项目列表页 + 新建项目弹窗
import {
  $, esc, api, toast, fail, fmtSize, fmtAge, fmtClock, on, pseg,
} from './util.js';

// 兜底值，正常会被 setMeta() 用 /api/kinds 的真实清单覆盖。
// 要与 projects.CATEGORIES 保持一致 —— 这只是「接口还没回来」时的占位，
// 但它错了会让人以为默认分类是这三个。
let CATEGORIES = ['新闻', '军事', '报道', '宣传'];
let KINDS = [];
let curFilter = '';          // 删完要按同一个筛选重载，否则列表会「跳回去」

export function setMeta(cats, kinds) {
  if (cats && cats.length) CATEGORIES = cats;
  if (kinds && kinds.length) KINDS = kinds;
  fillFilter();
}

// 分类筛选下拉。以前从来没被填过 —— #filterCategory 只有 HTML 里那一个
// 「全部分类」，所以这个筛选实际上是摆设（选不了任何具体分类）。
export function fillFilter() {
  const sel = $('#filterCategory');
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部分类</option>'
    + CATEGORIES.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  sel.value = cur;          // 停在原来的筛选上；那个分类没了就自动回到全部
}

export function fillNewDialog() {
  // datalist：给建议但不限制。分类是手输的，用 select 就没法建新分类。
  const dl = $('#newCatList');
  if (dl) {
    dl.innerHTML = CATEGORIES.map(
      (c) => `<option value="${esc(c)}"></option>`).join('');
  }
  const kinds = $('#newKinds');
  if (kinds && !kinds.dataset.built) {
    kinds.dataset.built = '1';
    kinds.innerHTML = KINDS.map((k, i) => `
      <label class="radio">
        <input type="radio" name="newkind" value="${esc(k.id)}" ${i === 0 ? 'checked' : ''}>
        <span><b>${esc(k.label)}</b><br><span class="muted small">${esc(k.hint)}</span></span>
      </label>`).join('');
  }
}

export async function load(filterCategory = '') {
  curFilter = filterCategory;
  const box = $('#projectList');
  const q = filterCategory ? `?category=${pseg(filterCategory)}` : '';
  let data;
  try {
    data = await api(`/api/projects${q}`);
  } catch (e) {
    fail(e);
    return [];
  }
  const items = data.projects || [];
  if (!items.length) {
    box.innerHTML = '<div class="empty">还没有项目。点右上「新建项目」开始。</div>';
    return items;
  }
  box.innerHTML = items.map((p) => `
    <div class="card proj" data-c="${esc(p.category)}" data-n="${esc(p.name)}">
      <div class="card-head">
        <span class="card-title">${esc(p.name)}</span>
      </div>
      <div class="muted small">
        ${esc(p.category)} · ${esc(kindLabel(p.kind))}
        ${p.images ? ` · 图片 ${p.images}` : ''}
        ${p.source ? ` · 原片 ${esc(fmtSize(p.source_size))}` : ''}
      </div>
      <div class="card-foot">
        <span class="muted small">
          ${p.output ? `成片 ${esc(fmtSize(p.output_size))} · ` : ''}${esc(fmtAge(Math.round(Date.now() / 1000 - p.mtime)))}
        </span>
        <span class="badge ${p.output ? 'ok' : 'mute'}">${p.output ? '有成片' : '空项目'}</span>
        <button class="btn btn-danger btn-del" data-del="1"
          title="删除整个项目文件夹">删除</button>
      </div>
    </div>`).join('');

  box.querySelectorAll('.proj').forEach((c) => {
    c.addEventListener('click', (e) => {
      // 点删除按钮不该顺手把项目打开 —— 那会让确认框随着导航一起消失
      if (e.target.closest('[data-del]')) return;
      location.hash = `#/p/${pseg(c.dataset.c)}/${pseg(c.dataset.n)}`;
    });
    c.querySelector('[data-del]').addEventListener('click', (e) => {
      e.stopPropagation();
      askDelete(c.dataset.c, c.dataset.n);
    });
  });
  return items;
}

// 删除是整个页面上唯一不可撤销的操作：整个项目文件夹一起走，成片、素材、
// 分析结果、导演文件全在内。所以不弹个「确定吗」就算 —— 要用户把项目名
// 敲一遍。误触的代价不是刷新一下就能回来的。
//
// **必须用弹窗，不能就地把卡片变成确认框**：卡片本身挂着「点了进项目」的
// 处理器，确认框里的输入框一点就会冒泡上去，页面直接跳进项目编辑页。
function askDelete(category, name) {
  const dlg = document.createElement('dialog');
  dlg.innerHTML = `
    <form method="dialog" class="dlg">
      <h3>删除项目</h3>
      <div class="warn inline">「${esc(name)}」整个项目文件夹都会被删掉
        —— 成片、素材、导演文件、分析结果全在内。<b>不可撤销。</b></div>
      <label>输入项目名以确认
        <input type="text" id="delConfirm" autocomplete="off"></label>
      <div class="dlg-foot">
        <button class="btn" value="cancel">取消</button>
        <button class="btn btn-danger" id="delGo" value="go" disabled>删除</button>
      </div>
    </form>`;
  document.body.appendChild(dlg);

  const input = dlg.querySelector('#delConfirm');
  const go = dlg.querySelector('#delGo');
  input.addEventListener('input', () => {
    go.disabled = input.value.trim() !== name;
  });
  dlg.addEventListener('close', async () => {
    dlg.remove();
    if (dlg.returnValue !== 'go') return;
    try {
      await api(`/api/projects/${pseg(category)}/${pseg(name)}`, { method: 'DELETE' });
      toast(`已删除「${name}」`);
      await load(curFilter);
    } catch (e) { fail(e); }
  });
  dlg.showModal();
  input.focus();
}

function kindLabel(id) {
  const k = KINDS.find((x) => x.id === id);
  return k ? k.label : (id || '未定类型');
}

export function bindNewProject(onCreated) {
  const dlg = $('#newDlg');
  on('#btnNewProject', 'click', () => {
    fillNewDialog();
    dlg.showModal();
  });
  on('#btnCreateProject', 'click', async (e) => {
    e.preventDefault();
    const name = $('#newName').value.trim();
    if (!name) { toast('项目名不能为空', 'bad'); return; }
    const kind = ($('input[name=newkind]:checked') || {}).value || 'director';
    const category = $('#newCategory').value.trim();
    if (!category) { toast('分类不能为空', 'bad'); return; }
    try {
      const p = await api('/api/projects', { body: { category, name, kind } });
      dlg.close();
      $('#newName').value = '';
      // 手输的分类多半是新的，立刻加进下拉。不然回到列表页时「全部分类」
      // 里没有它，想筛自己刚建的分类都选不到（刷新页面才会重新拉 /api/kinds）。
      if (!CATEGORIES.includes(category)) {
        CATEGORIES = CATEGORIES.concat(category);
        fillFilter();
      }
      toast('项目已创建');
      // 用户原话：「创建项目后应该进入里面」—— 直接跳进工作台
      onCreated(p);
    } catch (err) {
      fail(err);
    }
  });
}
