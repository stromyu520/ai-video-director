"""jobs.py — 单 worker 任务运行器

设计要点（每一条都是踩过的坑）：
- 子进程一律 argv 带 -u 且 env 里设 PYTHONUNBUFFERED=1：现有脚本全用 print，
  stdout 走管道时按块缓冲（4-8KiB），不加这个进度会攒到结束才到，
  基于「长时间无输出」的看门狗会误杀正常渲染。
- env 用 os.environ.copy() 再 update：env= 是替换不是合并，丢了 PATH
  会让管线内部所有裸名 ffmpeg 调用失败。
- start_new_session=True 让子进程自成进程组（pgid == pid），
  杀的时候 killpg 一次带走 ffmpeg 等孙进程。
- 单 worker：GPU 串行，且现有脚本对同 work_dir 并发不安全。
"""
import asyncio
import json
import os
import re
import shutil
import signal
import time
from pathlib import Path

from . import config
from .models import Job, StepSpec, StepState, MAX_LOG_LINES, TERMINAL, new_job_id
from .progress import ProgressTracker

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / 'video-env' / 'bin' / 'python')
JOBS_DIR = Path(__file__).resolve().parent / 'jobs'


def _job_env(job):
    """按任务注入的环境变量。目前只有一条：**配音音色**。

    两条管线读音色的方式不一样，所以两个变量都得注入：
      - 解说/二创：`render_remix.py` 直接读 `os.environ['REMIX_SPK']`；
      - 生成：`video_make_ffmpeg.py` 从 director.yaml 的 `voice.spk_id` 读，
        要覆盖它只能靠 `SPK_OVERRIDE`（见该文件里的覆盖分支）。
    少注入任何一个，页面上选完音色点渲染，成片里还是「故事女声」且毫无提示。
    """
    spk = (job.params or {}).get('spk_id')
    if not spk:
        return {}
    return {'REMIX_SPK': str(spk), 'SPK_OVERRIDE': str(spk)}


class StepFailed(Exception):
    pass


class StepStalled(Exception):
    pass


class EventHub:
    """job_id -> 订阅者队列。慢客户端丢最旧事件，不阻塞 worker。"""

    def __init__(self):
        self._subs = {}

    def subscribe(self, job_id):
        q = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(job_id, set()).add(q)
        return q

    def unsubscribe(self, job_id, q):
        s = self._subs.get(job_id)
        if s:
            s.discard(q)
            if not s:
                self._subs.pop(job_id, None)

    def publish(self, job_id, event):
        for q in list(self._subs.get(job_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()          # 丢最旧
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


class JobEmitter:
    """注入到 job.emit 上的发射器，供 fn 步骤（in-process 代码）回报进度。

    为什么不把 manager 直接塞进 job：fn 步骤只需要「写一行日志 / 报一下进度」，
    拿到整个 manager 就能 cancel/resume/submit —— 权限过大且容易写出环。

    工作线程安全：log/progress 允许从 asyncio.to_thread 的工作线程调用，
    内部统一转回事件循环线程，调用方不必自己 call_soon_threadsafe。
    """

    __slots__ = ('_mgr', '_job', '_loop')

    def __init__(self, mgr, job, loop):
        self._mgr, self._job, self._loop = mgr, job, loop

    def _on_loop(self, fn):
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            fn()                                    # 已在事件循环线程
        else:
            try:
                self._loop.call_soon_threadsafe(fn)  # 工作线程 → 事件循环
            except RuntimeError:
                pass                                # 循环已关闭（任务被取消）

    def log(self, line):
        job, mgr = self._job, self._mgr

        def go():
            mgr._emit(job, 'log', {'line': str(line)})
            mgr._append_log(job, str(line), job._seq)
        self._on_loop(go)

    def progress(self, frac, detail=''):
        """步骤内部进度 0..1 —— 只上不下（LLM 重试时不能让进度条回退）。"""
        job, mgr = self._job, self._mgr
        try:
            frac = max(0.0, min(1.0, float(frac)))
        except (TypeError, ValueError):
            return

        def go():
            cur = getattr(job, '_fn_progress', 0.0)
            if frac <= cur:
                return
            job._fn_progress = frac
            if detail:
                job.phase = str(detail)[:80]
            # fn 步骤没有 ProgressTracker，这里自己按步骤权重映射到全局
            specs = mgr.pipelines[job.pipeline].steps
            idx = next((i for i, s in enumerate(specs)
                        if s.key == job.step), None)
            if idx is None:
                return
            weights = [max(0.0, float(getattr(s, 'weight', 1.0) or 1.0))
                       for s in specs]
            total_w = sum(weights) or 1.0
            lo = sum(weights[:idx]) / total_w      # 同 _push_progress：
            hi = sum(weights[:idx + 1]) / total_w  # 两端都要除以 total_w
            job.progress = max(job.progress, lo + (hi - lo) * frac)
            mgr._emit(job, 'progress', {
                'progress': round(job.progress, 4),
                'phase': job.phase,
                'step': job.step,
                'heartbeat': False,
            })
        self._on_loop(go)

    def warning(self, msg):
        job = self._job

        def go():
            job.add_warning(str(msg))
        self._on_loop(go)


class JobManager:
    def __init__(self, pipelines, jobs_dir=JOBS_DIR):
        self.pipelines = pipelines
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.hub = EventHub()
        self._queue = asyncio.Queue()
        self._worker_task = None
        self._procs = {}                 # job_id -> proc
        self._gates = {}                 # job_id -> asyncio.Event
        self._resume_payload = {}
        self._locks = {}                 # work_dir -> asyncio.Lock（排队互斥）
        self._load_persisted()

    # ================= 持久化 =================
    def _job_path(self, job_id):
        return self.jobs_dir / f'{job_id}.json'

    def _log_path(self, job_id):
        return self.jobs_dir / f'{job_id}.log'

    def _load_persisted(self):
        for p in sorted(self.jobs_dir.glob('j-*.json')):
            try:
                job = Job.from_dict(json.loads(p.read_text(encoding='utf-8')))
            except Exception:
                continue
            if job.status in ('running', 'queued'):
                job.status = 'interrupted'
                job.error = '服务重启，任务中断'
                job.ended_at = job.ended_at or time.time()
            # JSON 是在最后一次 _emit 之前写的，seq 可能差一拍；
            # 以日志文件末行的 seq 为准，保证重启后 seq 不回退（否则重连客户端会收到重复 seq）
            job._seq = max(job._seq, self._last_log_seq(job.id))
            self.jobs[job.id] = job

    def _last_log_seq(self, job_id):
        path = self._log_path(job_id)
        if not path.exists():
            return 0
        try:
            with open(path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 4096))     # 只读尾部，日志可能很大
                tail = f.read().decode('utf-8', 'replace')
        except OSError:
            return 0
        for line in reversed(tail.splitlines()):
            head = line.split('\t', 1)[0]
            if head.isdigit():
                return int(head)
        return 0

    def _save(self, job):
        path = self._job_path(job.id)
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False),
                       encoding='utf-8')
        os.replace(tmp, path)

    def _append_log(self, job, line, seq):
        with open(self._log_path(job.id), 'a', encoding='utf-8') as f:
            f.write(f'{seq}\t{line}\n')

    # ================= 对外 API =================
    async def start(self):
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self):
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    def submit(self, pipeline_key, *, category, project, work_dir, params=None):
        spec = self.pipelines[pipeline_key]
        job = Job(
            id=new_job_id(), pipeline=pipeline_key,
            category=category, project=project,
            params=dict(params or {}, work_dir=str(work_dir)),
            cfg_snapshot=config.env_snapshot(),
        )
        job.steps = {s.key: StepState(key=s.key, label=s.label) for s in spec.steps}
        self.jobs[job.id] = job
        self._save(job)
        self._queue.put_nowait(job.id)
        self._emit(job, 'state', self.snapshot(job.id))
        return job

    def snapshot(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return None
        d = job.to_dict()
        d.pop('logs', None)
        d['step_specs'] = [
            {'key': s.key, 'label': s.label, 'kind': s.kind, 'optional': s.optional}
            for s in self.pipelines[job.pipeline].steps]
        d['elapsed'] = job.elapsed()
        return d

    def history(self, job_id, after=0, max_lines=500):
        """SSE 重放：只补发 log 事件（终端里的行）。

        契约：state/progress 是**快照**不是增量，重发旧的没有意义 ——
        调用方必须在重放后立刻补发一条当前 state + progress，让重连的客户端收敛。
        因此 job.logs（只存 log 事件）就是重放的完整事实来源；
        seq 是全局单调的，log 事件的 seq 会有空洞，客户端不得假设连续。
        优先用内存环形缓冲；缓冲覆盖不到 after 时回落 .log 文件。
        """
        job = self.jobs.get(job_id)
        if not job:
            return []
        # 内存缓冲完整覆盖 after 时才用它；否则回落到 .log 文件
        if job.logs and after >= job.logs[0]['seq']:
            return [ev for ev in job.logs if ev['seq'] > after][-max_lines:]
        out, path = [], self._log_path(job_id)
        if path.exists():
            for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
                if '\t' not in line:
                    continue
                seq_s, text = line.split('\t', 1)
                try:
                    seq = int(seq_s)
                except ValueError:
                    continue
                if seq > after:
                    out.append({'seq': seq, 'type': 'log', 'line': text})
        return out[-max_lines:]

    async def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if not job or job.is_terminal:
            return False
        # 先落状态，再杀进程。反过来会输掉一场竞态：_kill_group 内部 await
        # proc.wait()，读循环同时收到 EOF 并继续跑完，等不及这一句就把步骤
        # 判成 failed 了 —— 用户看到的是「任务已取消但这一步失败了」。
        job.status = 'cancelled'
        job.ended_at = time.time()
        self._save(job)
        app = self._gates.get(job_id)
        if app and not app.is_set():
            app.set()                       # 放行闸门，让 worker 走到取消检查
        proc = self._procs.get(job_id)
        if proc:
            await self._kill_group(proc)
        self._emit(job, 'state', self.snapshot(job_id))
        return True

    def resume(self, job_id, payload=None):
        """放行人工闸门（如二创关键词确认）。"""
        ev = self._gates.get(job_id)
        if not ev or ev.is_set():
            return False
        self._resume_payload[job_id] = payload or {}
        ev.set()
        return True

    # ================= 事件 =================
    def _emit(self, job, etype, data):
        job._seq += 1
        ev = {'seq': job._seq, 'type': etype, 'data': data}
        if etype == 'log':
            job.logs.append({'seq': job._seq, 'type': 'log',
                             'line': data.get('line', '')})
            if len(job.logs) > MAX_LOG_LINES:
                del job.logs[:len(job.logs) - MAX_LOG_LINES]
        self.hub.publish(job.id, ev)
        # state 事件很稀少（每步两三条），顺手落盘。
        # 这样任何 _emit(state) 都自带持久化，不必依赖调用点把 _save 写在后面 ——
        # 写在前面的话最后一拍（终态 state）永远存不进去，重启后 seq 比客户端少 1。
        # log 事件不落盘：一次渲染几千行，每行写一次 JSON 是不可接受的。
        if etype == 'state':
            self._save(job)

    # ================= worker =================
    async def _worker(self):
        while True:
            job_id = await self._queue.get()
            job = self.jobs.get(job_id)
            if not job or job.status in ('cancelled', 'interrupted'):
                continue
            try:
                await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:                      # noqa: BLE001
                job.status = 'failed'
                job.error = f'{type(e).__name__}: {e}'
                job.ended_at = time.time()
                self._save(job)
                self._emit(job, 'state', self.snapshot(job.id))

    async def _run_job(self, job):
        spec = self.pipelines[job.pipeline]
        job.status = 'running'
        job.started_at = time.time()
        self._save(job)
        self._emit(job, 'state', self.snapshot(job.id))
        wd = job.work_dir
        self._cleanup_orphans(wd)
        # fn 步骤（在服务器进程内跑的代码）靠这个回报进度和日志。
        # 不是 dataclass 字段，因此不会被 to_dict/asdict 序列化。
        job.emit = JobEmitter(self, job, asyncio.get_running_loop())

        for step_spec in spec.steps:
            state = job.steps[step_spec.key]
            job._fn_progress = 0.0          # 每步重置（权重映射的 local 值）
            if job.status == 'cancelled':
                state.status = 'cancelled'
                break
            job.step = step_spec.key
            # params.only = 只跑这几步。用在工作台上「只生成脚本先看一眼」
            # （渲染要几分钟 + 占显存，先审 yaml 再出片）。空 = 全跑。
            only = job.params.get('only')
            if only and step_spec.key not in only:
                state.status = 'skipped'
                state.ended_at = time.time()
                self._save(job)
                self._emit(job, 'state', self.snapshot(job.id))
                continue
            if step_spec.when is not None and not step_spec.when(job):
                # 例如「已有手写 director.yaml，不必调 LLM 生成」。
                state.status = 'skipped'
                state.ended_at = time.time()
                self._save(job)
                self._emit(job, 'state', self.snapshot(job.id))
                continue
            state.status = 'running'
            state.started_at = time.time()
            self._save(job)
            self._emit(job, 'state', self.snapshot(job.id))
            try:
                await self._run_step(job, step_spec, state)
            except StepStalled as e:
                state.status = 'failed'
                state.error = str(e)
                job.status = 'failed'
                job.error = str(e)
                break
            except StepFailed as e:
                if job.status == 'cancelled':
                    # 取消会让当前脚本以非 0 退出（_exec_script 抛 StepFailed），
                    # 但用户看到的不该是「这一步失败了」——整条任务是主动取消的。
                    state.status = 'cancelled'
                    break
                state.status = 'failed'
                state.error = str(e)
                if step_spec.optional:
                    job.status = 'done'
                    job.add_warning(f'可选步骤 {step_spec.label} 失败：{e}')
                else:
                    job.status = 'failed'
                    job.error = str(e)
                    break
            except asyncio.CancelledError:
                state.status = 'cancelled'
                raise
            else:
                state.status = 'done'
            finally:
                state.ended_at = time.time()
                self._save(job)
                self._emit(job, 'state', self.snapshot(job.id))

        if job.status == 'running':
            job.status = 'done'
        job.ended_at = time.time()
        job.progress = 1.0 if job.status == 'done' else job.progress
        if job.status == 'done':
            # 终态补一发 progress。最后一步的阶段表只到 0.97（进程还没退出，
            # 不该假装已完成），只看 progress 事件的客户端否则会永远停在 97.7%。
            self._emit(job, 'progress', {
                'progress': 1.0, 'phase': '完成', 'step': job.step,
                'heartbeat': False,
            })
        self._save(job)
        self._emit(job, 'state', self.snapshot(job.id))

    async def _run_step(self, job, spec, state):
        if spec.kind == 'fn':
            await spec.fn(job)
            return
        if spec.kind == 'gate':
            job.status = 'waiting'
            self._save(job)
            self._emit(job, 'state', self.snapshot(job.id))
            ev = asyncio.Event()
            self._gates[job.id] = ev
            try:
                await ev.wait()
            finally:
                self._gates.pop(job.id, None)
            if job.status == 'cancelled':
                return
            payload = self._resume_payload.pop(job.id, {})
            if spec.fn:
                await spec.fn(job, payload)
            job.status = 'running'
            self._save(job)
            self._emit(job, 'state', self.snapshot(job.id))
            return
        # script 步骤
        argv = spec.builder(job)
        await self._exec_script(job, spec, argv)

    async def _exec_script(self, job, spec, argv):
        # -u 是承重件：不加则 stdout 按块缓冲，进度与看门狗全废
        full_argv = [PYTHON, '-u'] + [str(a) for a in argv]
        proc = await asyncio.create_subprocess_exec(
            *full_argv,
            cwd=job.work_dir,
            env=config.build_env(extra=_job_env(job)),
            stdin=asyncio.subprocess.DEVNULL,   # 没有脚本能阻塞在 input() 上
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,             # setsid -> pgid == pid
            limit=1 << 20,
        )
        job.pids.append(proc.pid)
        self._procs[job.id] = proc
        self._emit(job, 'log', {'line': f'$ {" ".join(full_argv[1:])}'})

        spec_obj = self.pipelines[job.pipeline]
        tracker = ProgressTracker(
            phases=spec_obj.phases.get(spec.key, {}),
            expected_secs=spec.hard_timeout / 4,
        )
        started = time.time()
        ticker = asyncio.create_task(self._tick(job, tracker))
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(),
                                                 spec.idle_timeout)
                except asyncio.TimeoutError:
                    await self._kill_group(proc)
                    tail = '\n'.join(l['line'] for l in job.logs[-8:])
                    raise StepStalled(
                        f'卡死：{spec.idle_timeout:.0f}s 无输出，已终止进程组\n{tail}')
                except ValueError:
                    # 单行超过 limit
                    raw = await proc.stdout.read(65536)
                    if not raw:
                        break
                if not raw:
                    break
                line = raw.decode('utf-8', 'replace').rstrip('\n')
                self._emit(job, 'log', {'line': line})
                self._append_log(job, line, job._seq)
                if tracker.feed(line):
                    self._push_progress(job, tracker)
                if time.time() - started > spec.hard_timeout:
                    await self._kill_group(proc)
                    raise StepStalled(
                        f'超时：超过该步骤上限 {spec.hard_timeout:.0f}s，已终止')
        finally:
            ticker.cancel()
            self._procs.pop(job.id, None)
            if proc.pid in job.pids:
                job.pids.remove(proc.pid)

        rc = await proc.wait()
        if job.status == 'cancelled':
            raise StepFailed('已取消')
        if rc != 0:
            tail = '\n'.join(l['line'] for l in job.logs[-10:])
            raise StepFailed(f'退出码 {rc}\n{tail}')

    async def _tick(self, job, tracker):
        """每 2 秒推进一次插值进度，并推给前端（心跳）。"""
        while True:
            await asyncio.sleep(2.0)
            tracker.tick()
            self._push_progress(job, tracker, heartbeat=True)

    def _active_steps(self, job):
        """本次真正会跑的步骤，顺序与 execute 的循环一致。

        权重必须只在**会跑的步骤**上归一化。用全量 steps 的话，
        `only:['generate']` 这类筛选会让进度条卡在 21% 然后直接跳到 100%
        —— 因为分母里还躺着两个永远 skipped 的步骤。

        判据必须和 execute 里的两处 skip 逐字对齐（only 筛选 + when 谓词），
        两边一旦不一致，进度条就会说谎。
        """
        only = job.params.get('only')
        out = []
        for s in self.pipelines[job.pipeline].steps:
            if only and s.key not in only:
                continue
            if s.when is not None and not s.when(job):
                continue
            out.append(s)
        return out

    def _push_progress(self, job, tracker, heartbeat=False):
        # 每个步骤新建一个 tracker，local 值恒从 0 开始。直接取它会让
        # max() 把整条管线的进度钉死在第一步的终值上 —— 必须按步骤权重
        # 把 local 0..1 映射到该步骤在全局的区间。
        specs = self._active_steps(job)
        idx = next((i for i, s in enumerate(specs) if s.key == job.step), None)
        if idx is not None:
            weights = [max(0.0, float(getattr(s, 'weight', 1.0) or 1.0))
                       for s in specs]
            total_w = sum(weights) or 1.0
            # 两个端点都必须除以 total_w：hi 用累计和，不能拿已经除过的
            # lo 再加权重（那样跨度会被缩小成 w[idx]/total_w 被二次压缩）。
            lo = sum(weights[:idx]) / total_w
            hi = sum(weights[:idx + 1]) / total_w
            local = min(tracker.value, 1.0)
            job.progress = max(job.progress, lo + (hi - lo) * local)
        if tracker.detail or tracker.phase:
            job.phase = tracker.detail or tracker.phase
        eta = tracker.eta()
        self._emit(job, 'progress', {
            'progress': round(job.progress, 4),
            'phase': job.phase,
            'step': job.step,
            'elapsed': round(tracker.elapsed(), 1),
            'eta': round(eta, 1) if eta else None,
            'heartbeat': heartbeat,
        })

    # ================= 杀进程组 =================
    async def _kill_group(self, proc):
        """只杀子进程组，绝不杀自己。

        start_new_session=True 时 pgid == proc.pid，所以用 proc.pid，
        不能用 os.getpgid(0)（那是服务器自己的进程组）。
        """
        if proc.returncode is not None:      # 已回收，pid 可能已被复用
            return
        pgid = proc.pid
        if pgid == os.getpgid(0):
            return                            # 保险：永不自杀
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 5.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

    # ================= 清理 =================
    def _cleanup_orphans(self, work_dir, max_age=6 * 3600):
        """清掉项目目录里过期残留的临时目录（异常路径不会自清）。"""
        try:
            for p in Path(work_dir).glob('vm_*'):
                self._rm_if_old(p, max_age)
            for p in Path(work_dir).glob('rmx_*'):
                self._rm_if_old(p, max_age)
        except OSError:
            pass

    @staticmethod
    def _rm_if_old(path, max_age):
        try:
            if path.is_dir() and time.time() - path.stat().st_mtime > max_age:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    def is_work_dir_busy(self, work_dir):
        """同一 work_dir 已有未结束的任务？（并发会互踩输出文件）"""
        wd = str(Path(work_dir).resolve())
        for j in self.jobs.values():
            if j.is_terminal:
                continue
            if str(Path(j.work_dir).resolve()) == wd:
                return j.id
        return None
