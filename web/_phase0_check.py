"""_phase0_check.py — 阶段 0 的承重验证（用 video-env/bin/python 跑）

    ./video-env/bin/python web/_phase0_check.py

这一步不通过，后面全是空中楼阁。验的是：
  A stdout 缓冲    —— -u 让行实时到达，否则全攒到结束
  B 看门狗         —— 进程假死（SIGSTOP）时能否按静默阈值判定并终止
  C killpg         —— 一次带走子进程「及其孙进程」，5 秒内
  D SSE 重放       —— Last-Event-ID 补发 + 重启后 job 存活
  E 人工闸门       —— waiting 状态可被 /resume 放行并继续
  F 失败捕获       —— stderr 合并、退出码非 0 → job failed
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from web.jobs import JobManager                        # noqa: E402
from web.models import PipelineSpec, StepSpec          # noqa: E402
from web.progress import ProgressTracker               # noqa: E402

PY = str(ROOT / 'video-env' / 'bin' / 'python')
STEP = str(HERE / '_selftest_step.py')

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f'  {"✅" if ok else "❌"} {name}' + (f' — {detail}' if detail else ''))


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------- A 缓冲
async def test_buffering():
    print('\n[A] stdout 缓冲：-u 是否让行实时到达')
    results = {}
    for label, argv in (('无 -u', [PY, STEP, 'emit', '3']),
                        ('有 -u', [PY, '-u', STEP, 'emit', '3'])):
        env = os.environ.copy()
        env.pop('PYTHONUNBUFFERED', None)          # A/B 只差 -u 一个变量
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(
            *argv, env=env, stdout=asyncio.subprocess.PIPE)
        arrivals = []
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            arrivals.append(round(time.time() - t0, 2))
        await proc.wait()
        results[label] = arrivals
        print(f'  {label}: 行到达时刻 {arrivals}  (进程结束 {time.time()-t0:.2f}s)')

    plain, unbuf = results['无 -u'], results['有 -u']
    check('-u 下首行 < 1s 到达', bool(unbuf) and unbuf[0] < 1.0, f'{unbuf[:1]}')
    check('-u 下行是逐条到达的（不是一次性）',
          len(unbuf) >= 4 and unbuf[1] - unbuf[0] > 0.5, f'{unbuf}')
    check('无 -u 时确实全攒到最后（证明 -u 是承重件，不是迷信）',
          bool(plain) and len(plain) >= 4 and plain[0] > 3.0, f'{plain[:2]}')


# ------------------------------------------------------- 运行器辅助
def make_pipelines(steps, phases=None):
    return {'t': PipelineSpec(key='t', label='测试', steps=steps,
                              phases=phases or {})}


def script_step(key, argv, idle=5.0, hard=120.0, optional=False):
    return StepSpec(key=key, label=key, kind='script',
                    builder=lambda job, a=argv: a,
                    idle_timeout=idle, hard_timeout=hard, optional=optional)


async def run_job(mgr, steps, work_dir, phases=None, params=None):
    mgr.pipelines = make_pipelines(steps, phases)
    job = mgr.submit('t', category='其他', project='selftest',
                     work_dir=work_dir, params=params)
    for _ in range(600):                     # 最多等 60s
        if job.is_terminal or job.status == 'waiting':
            break
        await asyncio.sleep(0.1)
    return job


# ---------------------------------------------------------------- B 看门狗
async def test_watchdog(tmp):
    print('\n[B] 看门狗：进程假死（SIGSTOP）时按静默阈值终止')
    mgr = JobManager({}, jobs_dir=tmp / 'jobs')
    await mgr.start()
    t0 = time.time()
    job = await run_job(mgr, [script_step('s', [STEP, 'stop', '3'], idle=3.0)], tmp)
    dt = time.time() - t0
    check('job 判定为 failed', job.status == 'failed', job.status)
    check('错误信息是「卡死」而不是退出码', '卡死' in (job.error or ''),
          (job.error or '').splitlines()[0][:60])
    check('看门狗在 3~9s 内触发（no 输出阈值 3s + 宽限）', 3.0 <= dt <= 12.0,
          f'{dt:.1f}s')
    check('假死进程已被清掉', not job.pids or not any(alive(p) for p in job.pids),
          str(job.pids))
    check('进度已随日志推进', job.progress > 0, f'{job.progress:.3f}')
    await mgr.stop()


# ---------------------------------------------------------------- C killpg
async def test_killpg(tmp):
    print('\n[C] killpg：一次带走子进程及其孙进程')
    mgr = JobManager({}, jobs_dir=tmp / 'jobs')
    await mgr.start()
    t0 = time.time()
    job = await run_job(mgr, [script_step('s', [STEP, 'spawn'], idle=4.0)], tmp)
    dt = time.time() - t0
    lines = [l['line'] for l in job.logs]
    gp = next((int(l.split()[1]) for l in lines if l.startswith('grandchild ')), None)
    check('捕获到孙进程 pid', gp is not None, str(gp))
    check('主进程已终止', not any(alive(p) for p in job.pids), str(job.pids))
    if gp:
        await asyncio.sleep(0.5)
        check('孙进程也已消失（killpg 生效，不是只杀直接子进程）',
              not alive(gp), f'pid={gp}')
    check('整个终止流程 < 8s', dt < 8.0, f'{dt:.1f}s')
    await mgr.stop()


# ---------------------------------------------------------------- D SSE 重放
async def test_replay(tmp):
    print('\n[D] SSE 重放与重启后存活')
    jd = tmp / 'jobs'
    mgr = JobManager({}, jobs_dir=jd)
    await mgr.start()
    job = await run_job(mgr, [script_step('s', [STEP, 'markers'], idle=10.0)], tmp)
    total = job._seq
    check('job 正常完成', job.status == 'done', job.status)

    # 契约：history 只补发 log 事件（state/progress 是快照，由调用方补发最新的）
    mid = total - 3
    tail = mgr.history(job.id, after=mid)
    logs_after = [l for l in job.logs if l['seq'] > mid]
    check('按 Last-Event-ID 精确补发其后的 log 事件',
          len(tail) == len(logs_after), f'{len(tail)} vs {len(logs_after)}')
    check('补发事件 seq 单调递增且都 > after',
          all(e['seq'] > mid for e in tail)
          and [e['seq'] for e in tail] == sorted(e['seq'] for e in tail))
    check('只补发 log 事件，不含 state/progress（快照不重放）',
          all(e['type'] == 'log' for e in tail))

    # 内存缓冲被截断的情形：假装客户端从很早开始重连 → 必须回落 .log 文件
    job.logs = []
    fallback = mgr.history(job.id, after=0)
    check('内存缓冲为空时回落 .log 文件（重启后仍能补发）',
          len(fallback) > 5, f'{len(fallback)} 条')
    await mgr.stop()

    # 重启：新 manager 读同一目录
    mgr2 = JobManager({}, jobs_dir=jd)
    check('重启后 job 仍在注册表里', job.id in mgr2.jobs)
    check('重启后 seq 不回退（否则重连客户端会收到重复 seq）',
          mgr2.jobs[job.id]._seq == total, f'{mgr2.jobs[job.id]._seq} vs {total}')

    # running 态的任务重启后必须被标为 interrupted
    job.status = 'running'
    mgr._save(job)
    mgr3 = JobManager({}, jobs_dir=jd)
    check('重启时 running 任务被标为 interrupted',
          mgr3.jobs[job.id].status == 'interrupted', mgr3.jobs[job.id].status)


# ---------------------------------------------------------------- E 闸门
async def test_gate(tmp):
    print('\n[E] 人工闸门：waiting 可被 resume 放行')
    seen = {}

    async def on_gate(job, payload):
        seen.update(payload or {})

    steps = [script_step('a', [STEP, 'emit', '1'], idle=10.0),
             StepSpec(key='g', label='关键词确认', kind='gate', fn=on_gate),
             script_step('b', [STEP, 'emit', '1'], idle=10.0)]
    mgr = JobManager({}, jobs_dir=tmp / 'jobs')
    await mgr.start()
    job = await run_job(mgr, steps, tmp)
    check('停在闸门时状态为 waiting', job.status == 'waiting', job.status)
    check('闸门步骤标为 running', job.steps['g'].status == 'running')
    check('闸门后续步骤尚未开始', job.steps['b'].status == 'pending')

    check('resume 被接受', mgr.resume(job.id, {'terms': {'12': 'new term'}}))
    for _ in range(600):
        if job.is_terminal:
            break
        await asyncio.sleep(0.1)
    check('放行后任务跑完', job.status == 'done', f'{job.status} {job.error}')
    check('resume 载荷传到了闸门回调', seen.get('terms') == {'12': 'new term'},
          str(seen))
    await mgr.stop()


# ---------------------------------------------------------------- F 失败
async def test_failure(tmp):
    print('\n[F] 失败捕获：stderr 合并 + 非零退出码')
    mgr = JobManager({}, jobs_dir=tmp / 'jobs')
    await mgr.start()
    job = await run_job(mgr, [script_step('s', [STEP, 'boom'], idle=10.0)], tmp)
    lines = [l['line'] for l in job.logs]
    check('job 判定为 failed', job.status == 'failed', job.status)
    check('stderr 被合并进日志（否则报错原因看不见）',
          any('something went wrong' in l for l in lines))
    check('错误信息含退出码', '退出码 3' in (job.error or ''), job.error[:40])
    await mgr.stop()


# ---------------------------------------------------------------- G 进度解析
def test_progress():
    print('\n[G] ProgressTracker 解析真实标记')
    tr = ProgressTracker(phases={r'🎤 Phase 2/2': (0.10, 0.90)}, expected_secs=300)
    vals = []
    for line in ['🎬 场景1 平实 | 4.2s', '🎥 共 3 个镜头',
                 '🎤 Phase 2/2: 语音合成与字幕（3 场景）',
                 '🎬 场景1 x', '🎬 场景2 x', '🎬 场景3 x']:
        tr.feed(line)
        vals.append(round(tr.value, 3))
    check('进入 Phase 2 后进度跳到 0.10', vals[2] == 0.10, str(vals))
    check('场景级细化推进进度', vals[3] < vals[4] < vals[5], str(vals))
    check('进度单调递增', vals == sorted(vals), str(vals))
    check('场景计数到 3/3 时到达阶段末端（有真实分母，不算猜测）',
          tr.value == 0.90, f'{tr.value:.3f}')
    before = tr.value
    for _ in range(50):
        tr.tick()
    check('无输出时插值推进但永不后退', tr.value >= before, f'{before:.3f}→{tr.value:.3f}')

    # 插值（猜的）必须留 1% 余量；只有真实分母才允许爬满
    tr4 = ProgressTracker(phases={r'🎤 Phase 2/2': (0.10, 0.90)}, expected_secs=30)
    tr4.feed('🎤 Phase 2/2 x')
    tr4.phase_entered -= 300          # tick 走真实时钟，必须把时间倒回去模拟长静默
    for _ in range(5):
        tr4.tick()
    check('插值不越过本阶段上限（留 1% 给下一阶段）',
          tr4.value < 0.90 and tr4.value >= 0.89, f'{tr4.value:.4f}')
    tr2 = ProgressTracker(phases={r'X': (0.1, 0.9)})
    tr2.feed('X')
    check('未知阶段时 eta 为 None 而不抛异常', tr2.eta() is None or tr2.eta() >= 0)

    # 真实管线里最长的静默段一行标记都不打（逐镜头渲染循环），进度条不能是死的
    tr3 = ProgressTracker(phases={r'NEVER': (0.5, 0.9)}, expected_secs=60)
    tr3.phase_entered -= 30
    before3 = tr3.value
    tr3.tick()
    check('一个标记都没匹配到时也会热身爬升',
          before3 == 0 < tr3.value <= ProgressTracker.WARMUP_CEILING,
          f'{tr3.value:.3f}')


async def main():
    print(f'python  {sys.version.split()[0]}  ({sys.executable})')
    print(f'ffmpeg  {shutil.which("ffmpeg")}')
    tmp = Path(tempfile.mkdtemp(prefix='p0-'))
    try:
        await test_buffering()
        await test_watchdog(tmp)
        await test_killpg(tmp)
        await test_replay(tmp)
        await test_gate(tmp)
        await test_failure(tmp)
        test_progress()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f'\n{"="*56}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项')
    if FAIL:
        for f in FAIL:
            print(f'  ❌ {f}')
        return 1
    print('阶段 0 承重验证全部通过 —— 可以动真实管线了')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
