#!/usr/bin/env python3
"""ass_subtitle.py — SRT → ASS 转换，圆角半透明背景字幕

用法:
  python ass_subtitle.py <input.srt> [output.ass] [--offset 秒] [--style ass_rounded|ass_box]

原理:
  每 cue 生成两条 Dialogue 事件：
    背景行: \an7\pos + \p1 贝塞尔矢量圆角矩形（libass 0.15 不支持 \rnd 圆角标签）
    文字行: \an2 底部居中
  盒子几何用 PIL 实测文字宽度（混排数字/拉丁会窄于全角字，必须实测）。

样式对齐 MPT: FontSize=60 / Outline=1.5 / Shadow=0 / MarginV=48（≈底边 4.4%）
背景: PrimaryColour=&H8C000000&（alpha 反码，0x8C≈140/255 即 55% 不透明）
      radius=24（fs*0.4）、pad_x=24（fs*0.4）、pad_v=21（fs*0.35）
"""

import os, sys, re

FONT = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'

# ---------- 样式参数 ----------
PLAY_W, PLAY_H = 1920, 1080
FONT_SIZE    = 60        # MPT: font_size=60
OUTLINE      = 1.5       # 黑边（视频画面上可读）
MARGIN_V     = 48        # 底边距 ≈ 4.4%（MPT 为 5%）
RADIUS       = 24        # MPT: radius = max(8, fs*0.4)
PAD_X        = 24        # MPT: pad_x = fs*0.4
PAD_V        = 21        # MPT: vertical_padding = fs*0.35
LINE_H       = FONT_SIZE # \N 行距实测 = fontsize（libass 用字体行高，非 fs*1.2；2026-08-20 实测 60px）
PAD_SAFE_X   = 12        # PIL/libass 度量差异安全垫
PAD_SAFE_V   = 4
BG_ALPHA     = '&H8C000000&'   # 半透明黑（alpha 反码）
LINE_COLOR   = '&H00FFFFFF&'
OUTLINE_COL  = '&H00000000&'

STYLE_BODY = (
    f'Style: Sub,{FONT},'  # 占位，实际由 build_styles 生成
)

_ASS_ESCAPE_RE = re.compile(r'[{}]')


def _fmt_ass_time(sec):
    """秒 → ASS 时间戳 h:mm:ss.cc"""
    sec = max(0.0, sec)
    whole = int(sec)
    cs = int(round((sec - whole) * 100))
    if cs >= 100:      # 小数进位（如 .999 → 100 厘秒）
        cs = 0
        whole += 1
    h = whole // 3600
    m = (whole % 3600) // 60
    s = whole % 60
    return f'{h}:{m:02d}:{s:02d}.{cs:02d}'


def parse_srt(srt_text):
    """解析 SRT 文本 → [{start, end, text}]（秒）"""
    cues = []
    for block in re.split(r'\n\s*\n', srt_text.strip()):
        lines = block.split('\n')
        if len(lines) < 2:
            continue
        ts_line = None
        for i, l in enumerate(lines):
            if '-->' in l:
                ts_line = l
                text_lines = lines[i + 1:]
                break
        if ts_line is None:
            continue
        m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)', ts_line)
        if not m:
            continue
        a = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0
        b = int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7)) + int(m.group(8)) / 1000.0
        text = '\n'.join(text_lines).strip()
        if text and b > a:
            cues.append({'start': a, 'end': b, 'text': text})
    return cues


def srt_file_to_cues(path):
    with open(path, encoding='utf-8') as f:
        return parse_srt(f.read())


def _load_font(size=FONT_SIZE):
    from PIL import ImageFont
    return ImageFont.truetype(FONT, size)


def measure_text_width(text, font):
    """PIL 实测一行文字宽度（px）"""
    return font.getlength(text)


def _ass_escape(text):
    """ASS 文本转义：{ } 是控制字符，替换为全角"""
    return _ASS_ESCAPE_RE.sub(lambda m: '（' if m.group(0) == '{' else '）', text)


def rounded_rect_path(x0, y0, x1, y1, r):
    """圆角矩形矢量路径（\p1 后使用）

    用 b（三次贝塞尔）而非 q（二次贝塞尔）：libass 0.15.2 的 q 在 drawing
    里渲染为 45° 斜线（实测会斜切掉整个角），b 正常。这里把二次角
    P0→C→P1 精确转三次：C1 = P0+2/3(C-P0)，C2 = P1+2/3(C-P1)。
    """
    r = max(0, int(r))
    r3 = r // 3
    return (f'm {x0+r} {y0} l {x1-r} {y0} '
            f'b {x1-r3} {y0} {x1} {y0+r3} {x1} {y0+r} l {x1} {y1-r} '
            f'b {x1} {y1-r3} {x1-r3} {y1} {x1-r} {y1} l {x0+r} {y1} '
            f'b {x0+r3} {y1} {x0} {y1-r3} {x0} {y1-r} l {x0} {y0+r} '
            f'b {x0} {y0+r3} {x0+r3} {y0} {x0+r} {y0}')


def _wrap_lines(text, font, max_w):
    """超宽行按实测宽度贪心逐字换行；返回换行后的行列表"""
    lines = []
    for ln in text.split('\n'):
        if measure_text_width(ln, font) <= max_w or not ln:
            lines.append(ln)
            continue
        cur = ''
        for ch in ln:
            if cur and measure_text_width(cur + ch, font) > max_w:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
    return lines


def cues_to_ass(cues, style='ass_rounded', offset=0.0, play_w=PLAY_W, play_h=PLAY_H):
    """SRT cues → ASS 事件文本（不含 Script Info/Styles）

    offset: 秒，全局平移时间轴（场景内 cue 时间 + 场景起点）
    play_w/play_h: 画布尺寸（竖屏 9:16 传 1080x1920，跟随输出比例）
    返回 [(start, end, Dialogue行1, Dialogue行2)] 或 [(start, end, 单行)]
    """
    font = _load_font()
    margin_v = max(24, round(play_h * MARGIN_V / PLAY_H))   # 底边距按高度等比（4.4%）
    max_line_w = play_w - 2 * (PAD_X + PAD_SAFE_X)          # 可容纳的最长行宽
    out = []
    for cue in cues:
        start = cue['start'] + offset
        end = cue['end'] + offset
        if end <= start:
            continue
        t1 = _fmt_ass_time(start)
        t2 = _fmt_ass_time(end)
        # 长行自动换行（竖屏 1080 宽 60px 字约 16 字/行，超宽断行防出屏）
        lines = _wrap_lines(cue['text'], font, max_line_w)
        n = len(lines)

        # 文字行（\an2 底边锚定在 (W/2, H-MarginV)）
        text = '\\N'.join(_ass_escape(l) for l in lines)
        txt_ev = f'Dialogue: 0,{t1},{t2},Sub,,0,0,0,,{text}'

        if style == 'ass_box':
            # 兜底：BorderStyle=3 半透明直角背景（无圆角）
            out.append((start, end, txt_ev))
            continue

        # 盒子几何：PIL 实测最宽行
        max_w = max(measure_text_width(l, font) for l in lines)
        box_w = int(min(max_w + 2 * PAD_X + PAD_SAFE_X, play_w - 2 * PAD_X))
        box_h = n * LINE_H + 2 * (PAD_V + PAD_SAFE_V)
        # 背景行左上角：文字底边 = H-MarginV，顶边 = 底边 - n*LINE_H
        y_txt_top = play_h - margin_v - n * LINE_H
        bx = (play_w - box_w) // 2
        by = y_txt_top - PAD_V - PAD_SAFE_V
        path = rounded_rect_path(0, 0, box_w, box_h, RADIUS)
        bg_ev = (f'Dialogue: 0,{t1},{t2},BG,,0,0,0,,'
                 f'{{\\an7\\pos({bx},{by})\\p1}}{path}{{\\p0}}')
        out.append((start, end, bg_ev, txt_ev))
    return out


def build_ass_header(style='ass_rounded', play_w=PLAY_W, play_h=PLAY_H):
    """Script Info + Styles"""
    margin_v = max(24, round(play_h * MARGIN_V / PLAY_H))
    if style == 'ass_box':
        sub_style = (f'Style: Sub,WenQuanYi Zen Hei,{FONT_SIZE},'
                     f'{LINE_COLOR},{LINE_COLOR},{OUTLINE_COL},{BG_ALPHA},'
                     f'0,0,0,0,100,100,0,0,3,{OUTLINE},0,2,40,40,{margin_v},1')
        bg_style = None
    else:
        sub_style = (f'Style: Sub,WenQuanYi Zen Hei,{FONT_SIZE},'
                     f'{LINE_COLOR},{LINE_COLOR},{OUTLINE_COL},{OUTLINE_COL},'
                     f'0,0,0,0,100,100,0,0,1,{OUTLINE},0,2,40,40,{margin_v},1')
        bg_style = (f'Style: BG,WenQuanYi Zen Hei,{FONT_SIZE},'
                    f'{BG_ALPHA},{LINE_COLOR},{OUTLINE_COL},{OUTLINE_COL},'
                    f'0,0,0,0,100,100,0,0,1,0,0,2,0,0,0,1')
    hdr = [
        '[Script Info]',
        f'PlayResX: {play_w}',
        f'PlayResY: {play_h}',
        'WrapStyle: 0',
        'ScaledBorderAndShadow: yes',
        '',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
        sub_style,
    ]
    if bg_style:
        hdr.append(bg_style)
    hdr.append('')
    hdr.append('[Events]')
    hdr.append('Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text')
    return '\n'.join(hdr) + '\n'


def assemble_global_ass(scene_cues_list, scene_starts, ass_path, style='ass_rounded',
                        play_w=PLAY_W, play_h=PLAY_H):
    """多场景 cue 列表 + 场景全局起点 → 全片 ASS

    scene_cues_list: [[{start,end,text}], ...] 每场景的 cue（场景内相对时间）
    scene_starts:    [float, ...] 每场景全局起点（秒）
    play_w/play_h: 画布尺寸，必须与输出视频一致（libass 会按 PlayRes 等比缩放）
    返回 ass 文件路径
    """
    events = []
    for cues, off in zip(scene_cues_list, scene_starts):
        for ev in cues_to_ass(cues, style=style, offset=off,
                              play_w=play_w, play_h=play_h):
            events.append(ev[2])
            if len(ev) > 3:
                events.append(ev[3])
    body = build_ass_header(style, play_w, play_h) + '\n'.join(events) + '\n'
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write(body)
    return ass_path


def make_ass_from_srt(srt_path, ass_path, offset=0.0, style='ass_rounded',
                      play_w=PLAY_W, play_h=PLAY_H):
    """单场景 SRT → ASS（时间轴整体平移 offset 秒）"""
    cues = srt_file_to_cues(srt_path)
    events = []
    for ev in cues_to_ass(cues, style=style, offset=offset,
                          play_w=play_w, play_h=play_h):
        events.append(ev[2])
        if len(ev) > 3:
            events.append(ev[3])
    body = build_ass_header(style, play_w, play_h) + '\n'.join(events) + '\n'
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write(body)
    return ass_path


def main():
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <input.srt> [output.ass] [--offset 秒] [--style ass_rounded|ass_box]")
        sys.exit(1)
    srt_path = sys.argv[1]
    style = 'ass_rounded'
    offset = 0.0
    play_w, play_h = PLAY_W, PLAY_H
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == '--offset':
            offset = float(args[i + 1]); i += 2
        elif args[i] == '--style':
            style = args[i + 1]; i += 2
        elif args[i] == '--size':
            try:
                play_w, play_h = (int(v) for v in args[i + 1].lower().split('x'))
            except (ValueError, IndexError):
                print(f"⚠️ --size 格式应为 WxH，忽略: {args[i + 1]}")
            i += 2
        else:
            i += 1
    out = 'subtitle.ass'
    for a in sys.argv[2:]:
        if not a.startswith('--'):
            out = a
            break
    ass_path = make_ass_from_srt(srt_path, out, offset=offset, style=style,
                                 play_w=play_w, play_h=play_h)
    cues = srt_file_to_cues(srt_path)
    font = _load_font()
    print(f"✅ {ass_path}  ({len(cues)} cues, 字号{FONT_SIZE}, 样式{style})")
    if cues:
        w = measure_text_width(cues[0]['text'].split('\n')[0], font)
        print(f"   示例盒子: 文字宽≈{w:.0f}px → 盒子≈{int(w+2*PAD_X+PAD_SAFE_X)}x{len(cues[0]['text'].split(chr(10)))*LINE_H+2*(PAD_V+PAD_SAFE_V)}")
    return ass_path


if __name__ == '__main__':
    main()
