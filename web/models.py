"""models.py — 任务与步骤的数据模型

Job 是唯一的任务单位；一条管线由若干 StepSpec 组成。
进度状态（status）取值：
  queued | running | waiting | done | failed | cancelled | interrupted
  - waiting: 停在人工闸门（如二创关键词确认），等 /resume
  - interrupted: 服务重启时仍在 running 的任务
"""
import time
import uuid
from dataclasses import dataclass, field, asdict

TERMINAL = ('done', 'failed', 'cancelled', 'interrupted')


@dataclass
class StepState:
    key: str
    label: str = ''
    status: str = 'pending'          # pending|running|done|failed|skipped|cancelled
    started_at: float | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    error: str = ''

    @property
    def elapsed(self):
        if self.started_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)


@dataclass
class StepSpec:
    """一个步骤的定义。

    kind:
      'script' — 起子进程跑一个脚本，argv 由 builder(job) 给出
      'fn'     — 在服务器进程内跑一个 async 函数（如创建目录、拷贝）
      'gate'   — 挂起点，等人工确认后由 /resume 放行
    """
    key: str
    label: str = ''
    kind: str = 'script'
    builder: object = None           # Callable[[Job], list[str]] | None
    fn: object = None                # Callable[[Job], Awaitable[None]] | None
    when: object = None              # Callable[[Job], bool] | None，False 则跳过该步
    optional: bool = False
    idle_timeout: float = 300.0
    hard_timeout: float = 3600.0
    produces: tuple = ()
    # 该步骤在整条管线里的进度占比权重。步骤内进度是 0..1，靠这个映射到全局区间
    # （见 jobs._push_progress）。默认等权；渲染这类慢步骤调大。
    weight: float = 1.0


@dataclass
class PipelineSpec:
    key: str
    label: str
    steps: list = field(default_factory=list)
    # 标记正则 -> (阶段起始占比, 阶段结束占比)
    phases: dict = field(default_factory=dict)


def new_job_id():
    return f'j-{time.strftime("%Y%m%d")}-{uuid.uuid4().hex[:6]}'


@dataclass
class Job:
    id: str
    pipeline: str
    project: str = ''                # 项目目录名
    category: str = ''               # 新闻 | 其他 | 二创
    params: dict = field(default_factory=dict)
    status: str = 'queued'
    step: str = ''                   # 当前步骤 key
    steps: dict = field(default_factory=dict)   # key -> StepState
    progress: float = 0.0            # 0..1，单调递增
    phase: str = ''                  # 人类可读，如 "语音合成 3/8"
    logs: list = field(default_factory=list)    # 环形缓冲，供 SSE 重放
    exit_code: int | None = None
    error: str = ''
    warnings: list = field(default_factory=list)
    cfg_snapshot: dict = field(default_factory=dict)   # 绝不含 api_key
    pids: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    _seq: int = 0

    # ---------- 序列化 ----------
    def to_dict(self):
        d = asdict(self)
        d.pop('_seq', None)
        d['seq'] = self._seq          # 必须持久化：否则重启后 seq 归零，
        d['logs'] = self.logs[-MAX_LOG_LINES:]   # SSE 重连的 Last-Event-ID 会失配
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d.pop('_seq', None)
        seq = d.pop('seq', 0)
        raw_steps = d.pop('steps', {}) or {}
        job = cls(**{k: v for k, v in d.items()
                     if k in cls.__dataclass_fields__})
        job._seq = seq
        job.steps = {k: (v if isinstance(v, StepState) else StepState(**v))
                     for k, v in raw_steps.items()}
        return job

    # ---------- 便捷 ----------
    @property
    def work_dir(self):
        return self.params.get('work_dir', '')

    @property
    def is_terminal(self):
        return self.status in TERMINAL

    def elapsed(self):
        if self.started_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def add_warning(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)


MAX_LOG_LINES = 2000
