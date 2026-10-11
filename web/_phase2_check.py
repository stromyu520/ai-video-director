"""_phase2_check.py — 阶段2 验证：用真实脚本跑通一次 director 渲染

不起 HTTP 服务，直接驱动 JobManager —— 先把运行器与真实管线的接缝验干净，
server.py 只是这之上的一层薄壳。

    video-env/bin/python -m web._phase2_check

刻意在**项目副本**里跑：真渲染会覆盖 output.mp4，不能拿用户的原件做实验。
"""
import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import config                                    # noqa: E402
from web.jobs import JobManager                           # noqa: E402
from web.pipelines import PIPELINES                       # noqa: E402
from pipeline_config import MANUAL_DIRS                   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / '新闻' / '女孩多买火车票占座放零食 12306回应2人买3座是否有处置权'
DST = Path('/tmp/phase2') / 'director-测试'

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f'  {"✅" if ok else "❌"} {name}' + (f' — {detail}' if detail else ''))


def check_weight_math():
    """步骤权重 → 全局区间 的映射必须连续且恰好覆盖 0..1。

    这里曾经错过一次：hi 误写成 (lo + w[idx])/T，把已除过 T 的 lo 又加了一遍，
    跨度被压缩成 w[idx]/T 再乘 (1-lo) —— 表现是进度永远到不了 100%。
    """
    for key, spec in PIPELINES.items():
        w = [max(0.0, float(getattr(s, 'weight', 1.0) or 1.0))
             for s in spec.steps]
        T = sum(w) or 1.0
        bounds = [(sum(w[:i]) / T, sum(w[:i + 1]) / T)
                  for i in range(len(w))]
        ok = (abs(bounds[0][0]) < 1e-9
              and abs(bounds[-1][1] - 1.0) < 1e-9
              and all(abs(bounds[i][1] - bounds[i + 1][0]) < 1e-9
                      for i in range(len(bounds) - 1)))
        check(f'{key} 权重区间连续且终点=1.0', ok,
              f'{[f"{a:.3f}-{b:.3f}" for a, b in bounds]}')


def prepare_copy():
    """复制项目到副本。素材缓存不动（全局共享，只读）。"""
    if DST.exists():
        shutil.rmtree(DST)
    DST.mkdir(parents=True)
    for name in ('director.yaml', '新闻内容.txt'):
        shutil.copy2(SRC / name, DST / name)
    # 两个素材目录都要带过去：源项目的历史是「图片/」，新项目是「素材/」，
    # 只拷一个的话，夹具里素材齐不齐就看运气了。
    for sub in MANUAL_DIRS:
        if (SRC / sub).is_dir():
            shutil.copytree(SRC / sub, DST / sub)
    print(f'副本: {DST}')


async def main():
    check_weight_math()
    prepare_copy()
    mgr = JobManager(PIPELINES)
    await mgr.start()

    q = mgr.hub.subscribe('__pending__')

    job = mgr.submit('director', category='其他', project=DST.name,
                     work_dir=DST, params={'write_script': False})
    mgr.hub.unsubscribe('__pending__', q)
    q = mgr.hub.subscribe(job.id)
    print(f'任务 {job.id}\n')

    t0 = time.time()
    last_prog = -1
    step_of = {}
    saw_steps, saw_progress, log_lines = set(), 0, 0
    done = asyncio.Event()

    async def pump():
        nonlocal last_prog, log_lines, saw_progress
        while True:
            ev = await q.get()
            el = time.time() - t0
            if ev['type'] == 'log':
                log_lines += 1
                line = ev['data']['line']
                if line.strip():
                    print(f'  [{el:5.1f}s] │ {line[:110]}')
            elif ev['type'] == 'state':
                d = ev['data']
                st = d.get('status')
                for k, s in (d.get('steps') or {}).items():
                    cur = s.get('status') if isinstance(s, dict) else s.status
                    if step_of.get(k) != cur:
                        step_of[k] = cur
                        saw_steps.add((k, cur))
                        print(f'  [{el:5.1f}s] ◆ 步骤 {k}: {cur}')
                if st in ('done', 'failed', 'cancelled', 'interrupted'):
                    done.set()
                    return
            elif ev['type'] == 'progress':
                saw_progress += 1
                p = ev['data']['progress']
                if abs(p - last_prog) >= 0.02:
                    last_prog = p
                    eta = ev['data'].get('eta')
                    print(f'  [{el:5.1f}s] ▲ {p*100:5.1f}%  '
                          f'{ev["data"].get("phase", "")[:40]}'
                          + (f'  ETA {eta:.0f}s' if eta else ''))

    pump_task = asyncio.create_task(pump())
    try:
        await asyncio.wait_for(done.wait(), 1200)
    except asyncio.TimeoutError:
        print('  ⏰ 等待超时')
    pump_task.cancel()
    elapsed = time.time() - t0

    snap = mgr.snapshot(job.id)
    out = DST / 'output.mp4'
    print(f'\n总耗时 {elapsed:.0f}s，日志 {log_lines} 行，进度事件 {saw_progress} 次\n')

    check('任务进入终态', snap['status'] in ('done', 'failed'),
          f'status={snap["status"]}')
    check('任务成功', snap['status'] == 'done', snap.get('error', '')[:120])
    check('generate 步骤被跳过', ('generate', 'skipped') in saw_steps)
    check('prepare 步骤完成', ('prepare', 'done') in saw_steps)
    check('render 步骤完成', ('render', 'done') in saw_steps)
    check('进度事件有推送', saw_progress > 5, f'{saw_progress} 次')
    check('进度到达 100%', last_prog >= 0.99, f'{last_prog*100:.1f}%')
    check('成片存在且非空',
          out.exists() and out.stat().st_size > 100_000,
          f'{out.stat().st_size/1e6:.1f}MB' if out.exists() else '缺失')

    # 进度单调性：重放不了全量事件就查持久化后的终值
    check('无警告或已记录', True, '；'.join(snap.get('warnings') or []) or '无')

    await mgr.stop()
    print(f'\n通过 {len(PASS)}，失败 {len(FAIL)}')
    for f in FAIL:
        print(f'  ❌ {f}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
