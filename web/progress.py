"""progress.py — 把脚本的 print 输出解析成进度

现有脚本没有任何机器可读进度，只有 emoji 前缀的 print 行。这里：
- 阶段标记 → 映射到 [0,1) 区间（phases 表由 pipelines.py 提供）
- 阶段内部用已知分母细化（如 "场景3/8"）
- 没有任何输出时用 creep() 指数插值，保证进度条会动
- 进度**单调递增**，永不后退
"""
import math
import re
import time


class ProgressTracker:
    """跟踪单个步骤内的进度。

    phases: {正则: (start_frac, end_frac)}，按顺序匹配；命中后进入该阶段。
    expected_secs: 该步骤预期总耗时（用于 creep 的 τ 与剩余时间估算）。
    """

    def __init__(self, phases=None, expected_secs=120.0):
        self.phases = list((phases or {}).items())
        self.expected_secs = max(20.0, float(expected_secs or 120.0))
        self.value = 0.0
        self.phase = ''
        self.phase_start = 0.0
        self.phase_end = 0.0
        self.phase_entered = time.time()
        self.step_started = time.time()
        self.detail = ''
        self.total_units = 0          # 已知分母（如总场景数）
        self.done_units = 0

    # ---------- 标记解析 ----------
    def feed(self, line):
        """喂一行 stdout，返回 True 表示进度有更新。"""
        before = (self.value, self.phase, self.detail)

        m = re.search(r'共\s*(\d+)\s*个镜头', line)
        if m:
            self.total_units = max(self.total_units, int(m.group(1)))

        if 'Phase 2/2' in line:
            m2 = re.search(r'（(\d+)\s*场景）', line)
            if m2:
                self.total_units = int(m2.group(1))

        # 场景总数：plan_remix / render_remix 打的是「场景数: 7」，与
        # video_make 的「（7 场景）」写法不同，两个都得认。
        m = re.search(r'场景数[:：]\s*(\d+)', line)
        if m:
            self.total_units = max(self.total_units, int(m.group(1)))

        for pattern, (lo, hi) in self.phases:
            if re.search(pattern, line):
                self._enter_phase(line, lo, hi)
                break

        # 逐项计数：[3/7] —— tts_express 的批量合成与 resolve_materials 的
        # 逐场景下载都打这个格式。用它自己的分母，不与「场景N」共用 done_units：
        # 批量走到 [7/7] 后再遇到「场景1」，会显示成「场景 7/7」。
        m = re.match(r'\s*\[(\d+)\s*/\s*(\d+)\]', line)
        if m:
            self._refine(int(m.group(1)), int(m.group(2)),
                         f'{m.group(1)}/{m.group(2)}')

        # 场景级细化：🎬 场景3 平实 | 4.2s / 🎤 场景3: TTS [故事女声]
        m = re.search(r'场景\s*(\d+)', line)
        if m and self.total_units > 0:
            self._refine(int(m.group(1)), self.total_units,
                         f'场景 {m.group(1)}/{self.total_units}')

        return (self.value, self.phase, self.detail) != before

    def _refine(self, done, total, label):
        """把 done/total 按**当前阶段**的区间映射成进度。"""
        if total <= 0:
            return
        span = self.phase_end - self.phase_start
        self._set(max(self.phase_start + span * min(1.0, done / total),
                      self.value))
        self.detail = label

    def _enter_phase(self, line, lo, hi):
        if hi <= self.value:
            # 已越过的阶段。重入不会让 value 回退（_set 单调），但会把
            # phase_end 拉到身后，此后 tick() 一直被压在它下面 —— 进度就死了。
            return
        label = re.sub(r'^[\s\W_]+', '', line).strip()
        self.phase = label[:60]
        self.phase_start, self.phase_end = float(lo), float(hi)
        self.phase_entered = time.time()
        self._set(max(lo, self.value))

    def _set(self, v):
        # 单调递增
        self.value = max(self.value, min(float(v), 1.0))

    # ---------- 无输出时的插值 ----------
    # 还没匹配到任何阶段标记时能爬到的上限。留在这里是因为真实管线里
    # 最长的静默段（逐镜头渲染循环）可能一行标记都不打，进度条不能是死的。
    WARMUP_CEILING = 0.20

    def tick(self):
        """定时调用：上一阶段结束、下一阶段未开始时插值，避免进度条僵住。"""
        tau = max(20.0, self.expected_secs / 3.0)
        frac = 1.0 - math.exp(-(time.time() - self.phase_entered) / tau)

        if self.phase_end <= self.phase_start:      # 一个阶段标记都还没见到
            self._set(min(frac, 1.0) * self.WARMUP_CEILING)
            return self.value

        span = self.phase_end - self.phase_start
        # 只爬到本阶段上限的 1% 之前：既不假装知道，也给下一阶段留出可见的位移
        self._set(min(self.phase_start + span * frac, self.phase_end - 0.01))
        return self.value

    # ---------- 时间估算 ----------
    def elapsed(self):
        return time.time() - self.step_started

    def eta(self):
        """粗略剩余秒数；progress 太小时不给。"""
        if self.value < 0.03:
            return None
        total = self.elapsed() / self.value
        return max(0.0, total - self.elapsed())
