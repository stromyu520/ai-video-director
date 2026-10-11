#!/usr/bin/env python3
"""_agent_check.py — director_agent 校验器的回归测试

    ./video-env/bin/python _agent_check.py

为什么要专门测校验器：它是「LLM 输出」和「渲染器契约」之间的唯一闸门。
两类错误的代价不对称 ——
  漏报：渲染器裸 KeyError / 素材静默降级成文字卡片（成片 28MB→1.4MB）；
  误报：把已经合格的脚本反复打回重写，用户看到的是「怎么老生成不出来」。
本文件两边都测，尤其**把项目里 4 个既有 director.yaml 全喂进来**
（test_real_scripts_pass），它们只许报「已登记的历史缺陷」。

**片长这条规则的期望值一律从 speech_rate 现算，不在本文件里写死数字。**
写死就等于把「4.5 字/秒」这个错误常数换个地方再抄一遍 —— 本文件第一版
正是这么翻的：拿 3.5~9.0（±2.5 倍）当护栏，而那个区间是在**错误的分母**
（字数 ÷ 声明时长）上标定的，结果片子比要求长 75% 也照样放行。
现在率随音色实测、样本自动累加，期望值必须跟着动。
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import yaml                                                     # noqa: E402
import director_agent as A                                      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    mark = '✅' if cond else '❌'
    print(f'  {mark} {name}' + (f'  —— {detail}' if detail and not cond else ''))


_CN = '零一二三四五六七八九'


def errs_for(text, work_dir=HERE, dur=30, **kw):
    return A.validate_director(text, work_dir, dur, **kw)[0]


def scene(i, narration, source='搜索:train station', vtype='video', **kw):
    sc = {'id': i, 'narration': narration, 'spoken': narration,
          'mood': '平实', 'speed': '中',
          'visuals': {'type': vtype, 'source': source}}
    sc.update(kw)
    return sc


def body(scenes, **over):
    d = {'project': {'title': '测试', 'duration': 30, 'type': '新闻'},
         'voice': {'spk_id': '故事女声', 'express': True, 'speed': '中'},
         'optimize': False, 'scenes': scenes,
         'bgm': {'path': ''},
         'output': {'subtitle': True, 'size': [1080, 1920]}}
    d.update(over)
    return yaml.safe_dump(d, allow_unicode=True, sort_keys=False)


def text_of(n, per=25):
    """n 个场景、每场景 per 字。"""
    return body([scene(i, '火' * per) for i in range(1, n + 1)])


def scenes_exact(total, n=7):
    """n 个场景、旁白合计正好 total 字（用来精确压在区间边界上）。"""
    per, rem = divmod(int(total), n)
    return [scene(i, '火' * (per + (1 if i <= rem else 0)))
            for i in range(1, n + 1)]


def text_exact(total, n=7):
    return body(scenes_exact(total, n))


def budget(voice='故事女声', dur=30):
    """这个音色在这个片长下的 (目标字数, 下界, 上界) —— 现算，别抄。"""
    mid = int(round(dur * A.speech_rate.rate_for(voice)))
    return mid, int(mid * (1 - A.RATE_TOL)), int(mid * (1 + A.RATE_TOL))


# ---------------------------------------------------------------------------

# 既有脚本的**已知缺陷**。它们不是回归，是历史遗留：
# 要么写于校验器存在之前，要么是人手写的、没走 agent。
# 断言「报出来的错误是这些的子集」—— 有新错误才是回归。
#
# '应按' = 片长偏差。这几个文件声明 30s，旁白却按老习惯写了 168~235 字，
# 按实测 4.6 字/秒念出来是 36~51s —— **这正是用户报的那个问题**
# （「我要 30 秒，出来的片子 43 秒」）。它们现在被判超预算是**预期的**，
# 不是新 bug；文件在盘上不动、渲染照跑，只有重新生成脚本时才会被要求改写。
KNOWN_DEFECTS = {
    '女孩多买火车票占座放零食 12306回应2人买3座是否有处置权': ['应按'],
    '智能剪辑二创演示': ['应按'],
    # 6 个搜索词全是中文 —— Pexels 只认英文，这些场景会静默降级成文字卡片
    '日本第一位女性首相修法堵死女性天皇之路': ['非 ASCII', '应按'],
    # 手写遗留：只有 3 个场景，且引用了 图片/主视觉.jpg、图片/外观.png（目录里没有）
    '标致508L': ['场景数', '本地素材不存在'],
}


def test_real_scripts_pass():
    """既有脚本只许因已知原因报错 —— 出现新错误即回归。"""
    print('\n[真实脚本：只许报已知缺陷，不许有新错误]')
    hits = sorted(glob.glob(os.path.join(HERE, '新闻/*/director.yaml'))) + \
        sorted(glob.glob(os.path.join(HERE, '其他/*/director.yaml')))
    # 浏览器套件会临时建 PW* 项目再删；它是**测试产物**，不是「既有脚本」，
    # 内容归 e2e.js 管。不排除的话，跑完 e2e 再跑本文件就会多出一条红。
    hits = [f for f in hits
            if not os.path.basename(os.path.dirname(f)).startswith('PW')]
    check('找到既有 director.yaml', len(hits) >= 3, f'只有 {len(hits)} 个')
    for f in hits:
        name = os.path.basename(os.path.dirname(f))
        raw = open(f, encoding='utf-8').read()
        parsed = yaml.safe_load(raw)
        dur = (parsed.get('project') or {}).get('duration') or 30
        errs = errs_for(raw, os.path.dirname(f), dur)
        allowed = KNOWN_DEFECTS.get(name)
        if allowed is None:
            check(f'{name[:30]}（未登记，按「应全过」要求）', not errs,
                  '; '.join(errs))
            continue
        unexpected = [e for e in errs
                      if not any(a in e for a in allowed)]
        check(f'{name[:30]} 无新错误', not unexpected,
              f'已知 {allowed or "无"}；新出现: {unexpected}')


def test_accepts_good():
    print('\n[合格输入：不能误报]')
    mid, lo, hi = budget()
    check(f'正中预算 {mid} 字', not errs_for(text_exact(mid)))
    check(f'贴着下界 {lo} 字（含边界）', not errs_for(text_exact(lo)))
    check(f'贴着上界 {hi} 字（含边界）', not errs_for(text_exact(hi)))

    def same_per_scene(**over):
        return [scene(i, '火' * (mid // 7), **over) for i in range(1, 8)]

    check('type:text 的 source 是大字文案（不是路径）',
          not errs_for(body(same_per_scene(source='关注我，掌握趋势',
                                           vtype='text'))))
    check('text 场景 source 为空',
          not errs_for(body(same_per_scene(source='', vtype='text'))))
    check('全角冒号搜索词',
          not errs_for(body(same_per_scene(source='搜索：train station'))))

    # 预算必须随音色走 —— 这就是「按音色实测」的全部意义。
    # 写死一个常数的话，换线上音色就得靠人手重调，等于没校准。
    c_local = A.speech_rate.chars_for('故事女声', 30)
    c_edge = A.speech_rate.chars_for('edge:zh-CN-XiaoxiaoNeural', 30)
    check(f'30s 预算随音色变（本地 {c_local} 字 / 线上 {c_edge} 字）',
          c_local != c_edge)


def test_rejects_bad():
    print('\n[不合格输入：必须抓到]')

    def one(name, scenes, expect_in, dur=30, **over):
        errs = errs_for(body(scenes, **over), dur=dur)
        hit = any(expect_in in e for e in errs)
        check(name, hit, f'期望含「{expect_in}」，实际: {errs or "（无错误）"}')

    mid, lo, hi = budget()
    # 底稿用正中预算，这样下面每条测的都是它自己那条规则 ——
    # 底稿本身就超预算的话，'旁白远超片长' 之外的用例会一并报片长错误，
    # 断言虽然是集合包含、照样能过，但读日志的人会以为规则串了。
    good = scenes_exact(mid)

    one('缺 project.title', good, 'project.title',
        project={'duration': 30})
    one('缺 voice.spk_id', good, 'voice.spk_id', voice={'express': True})
    # good[1:] 已经从 id=2 起，再加一个 id=1 才是真重复
    one('场景 id 重复', [scene(1, '火' * 25)] + good, 'id=1 与前面重复')
    one('narration 为空', [scene(i, '   ' if i == 3 else '火' * 25)
                           for i in range(1, 8)], 'narration 为空')
    one('narration 与 spoken 是两份文案（无数字可解释）',
        [dict(scene(i, '火' * 25), spoken='水' * 25) for i in range(1, 8)],
        '两份不同的文案')
    # 反过来：有数字时差异是合法的（spoken 是数字转中文的朗读版），不能报。
    # 注意字数要够 —— 太短会撞上字数速率护栏，那样测的就不是这条规则了。
    check('有数字的 narration/spoken 差异不算错',
          not errs_for(body([
              {'id': i, 'narration': f'第{i}代车型售价十六万起，全系标配十项',
               'spoken': f'第{_CN[i]}代车型售价十六万起，全系标配十项',
               'mood': '平实', 'speed': '中',
               'visuals': {'type': 'video', 'source': '搜索:car interior'}}
              for i in range(1, 8)])))
    one('场景数过多', [scene(i, '火' * 25) for i in range(1, 13)], '场景数 12')
    one('场景数过少', [scene(i, '火' * 25) for i in range(1, 4)], '场景数 3')
    one('中文搜索词（Pexels 只认英文）',
        [scene(i, '火' * 25, source='搜索:日本 国会 议事堂') for i in range(1, 8)],
        '非 ASCII')
    one('搜索词只有 1 个词',
        [scene(i, '火' * 25, source='搜索:train') for i in range(1, 8)],
        '1 个词')
    one('搜索词 5 个词',
        [scene(i, '火' * 25, source='搜索:a b c d e') for i in range(1, 8)],
        '5 个词')
    one('引用不存在的本地素材',
        [scene(i, '火' * 25, source='图片/不存在.jpg') for i in range(1, 8)],
        '本地素材不存在')
    # 片长：提示语要指明往哪边改，别只说「超了」
    one('旁白远超片长', [scene(i, '火' * 90) for i in range(1, 8)], '删减冗余表达')
    one('旁白远低于片长', [scene(i, '火' * 5) for i in range(1, 8)], '补充细节')
    # 边界外一格就该拦下 —— ±15% 收窄的意义全在这两条上
    one(f'高出上界 1 字（{hi + 1} 字）', scenes_exact(hi + 1), '删减冗余表达')
    one(f'低于下界 1 字（{lo - 1} 字）', scenes_exact(lo - 1), '补充细节')

    print('\n[坏 YAML]')
    check('不是 YAML', errs_for('{这不是 yaml: ['))
    check('顶层不是映射', errs_for('- 1\n- 2\n'))
    check('scenes 为空', errs_for(body([])))


def test_no_search():
    """关了「自动搜索」：禁 `搜索:`、场景数下限跟着本地素材走。

    这条不是风格偏好。关了搜索还写着 `搜索:`，渲染时那个场景**不联网**，
    只会退成文字卡 —— 而用户明确选的是「宁可片子短」。必须在生成侧拦下，
    让它重写成「少排一场」，别等成片里才发现多了几张字卡。
    """
    print('\n[关了自动搜索：禁 搜索: / 少排几场]')
    mid, lo, hi = budget()

    # 先钉基线：同一份稿子开着搜索是合格的。少了这一步，下面测到的
    # 可能是片长或别的规则，而不是这条 —— 断言照样绿，规则却没人守着。
    check('基线：开着搜索，全 搜索: 的稿子通过', not errs_for(text_exact(mid)))

    errs = errs_for(text_exact(mid), allow_search=False)
    n_hit = sum(1 for e in errs if '关闭了自动搜索' in e)
    check('关了搜索：7 个 搜索: 场景全被判错', n_hit == 7,
          f'只报出 {n_hit} 条；实际: {errs[:3]}')

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = os.path.join(td, '素材')
        os.makedirs(d)
        names = ['前脸', '侧面', '内饰']
        for n in names:
            open(os.path.join(d, f'{n}.jpg'), 'wb').close()

        check('local_material_count 数到 3 张', A.local_material_count(td) == 3,
              str(A.local_material_count(td)))
        open(os.path.join(td, '根目录图.png'), 'wb').close()
        check('根目录图片也算（口径与 scan_materials 一致）',
              A.local_material_count(td) == 4)
        open(os.path.join(d, '空镜.mp4'), 'wb').close()
        check('素材/ 里的视频也算（否则「传了视频却说没素材」）',
              A.local_material_count(td) == 5)

        # 3 张图排 3 场：默认下限 4 该拦，关了搜索、下限降到 3 该放行。
        three = [scene(i, '火' * (mid // 3), source=f'素材/{n}.jpg', vtype='image')
                 for i, n in enumerate(names, 1)]
        raw3 = body(three)
        check('3 场：默认下限 4 → 报场景数',
              any('场景数 3' in e for e in errs_for(raw3, work_dir=td)))
        check('3 场：关了搜索、下限 3 → 通过（少排几场是允许的）',
              not errs_for(raw3, work_dir=td, allow_search=False, scenes_min=3),
              '; '.join(errs_for(raw3, work_dir=td, allow_search=False,
                                 scenes_min=3)[:2]))
        check('2 场：低于下限 3 仍然报错（下限不是「随便几场都行」）',
              any('场景数 2' in e for e in errs_for(
                  body(three[:2]), work_dir=td, allow_search=False,
                  scenes_min=3)))

        # 提示词侧：关了就得不提「搜索:」，否则等于给模型发许可，
        # 校验器再把它的输出全部打回 —— 白烧三轮重试。
        mats = ['  本地图片: 素材/前脸.jpg']
        off = A.build_prompt('T', '正文', mats, 30, total_chars=mid,
                             allow_search=False)
        on = A.build_prompt('T', '正文', mats, 30, total_chars=mid)
        check('关了搜索：提示词里带上禁用规则', '本次关闭了自动搜索' in off)
        check('开着搜索：不加那段规则', '本次关闭了自动搜索' not in on)
        off0 = A.build_prompt('T', '正文', [], 30, total_chars=mid,
                              allow_search=False)
        check('关了搜索且清单为空：不再说「全部用 搜索: 自动获取」',
              '全部用' not in off0)


def test_summarize():
    """生成成功后的最后一步，纯粹但**一定会走到** —— 它炸了等于白跑一轮 LLM。

    实际翻过：常数块被换掉时漏了这里引用的 CHARS_PER_SEC，
    于是每次生成到收尾都 NameError。这条用例就是为它加的。
    """
    print('\n[summarize：别让常数缺失炸在收尾]')
    doc = yaml.safe_load(text_of(7, 25))
    info = A.summarize(doc, '/tmp/x.yaml')
    check('字数对得上', info['chars'] == 175, str(info))
    check('场景数对得上', info['scenes'] == 7, str(info))
    want = A.speech_rate.dur_for(doc['voice']['spk_id'], 175)
    check(f'预估秒数按该音色实测（{want}s）', info['eta_sec'] == want, str(info))


def test_apply_defaults():
    print('\n[apply_defaults：页面上选的音色必须赢过 LLM 写的]')
    p = yaml.safe_load(text_of(7))
    A.apply_defaults(p, title='T', dur=45, size='16:9',
                     spk_id='edge:zh-CN-XiaoxiaoNeural', express=False)
    check('spk_id 被覆盖', p['voice']['spk_id'] == 'edge:zh-CN-XiaoxiaoNeural')
    check('express 被覆盖', p['voice']['express'] is False)
    check('duration 被覆盖', p['project']['duration'] == 45)
    check('size 16:9 -> [1920,1080]', p['output']['size'] == [1920, 1080])
    check('optimize 强制 false', p['optimize'] is False)

    p2 = {}
    A.apply_defaults(p2, title='X', dur=30, size='9:16')
    check('空 dict 也能补全', p2['voice']['spk_id'] == '故事女声'
          and p2['output']['size'] == [1080, 1920])


def test_extract_yaml():
    print('\n[extract_yaml]')
    check('```yaml 围栏', A.extract_yaml('说明\n```yaml\nproject: 1\n```\n尾巴')
          == 'project: 1')
    check('裸 ``` 围栏', A.extract_yaml('```\nproject: 1\n```') == 'project: 1')
    check('无围栏', A.extract_yaml('  project: 1  ') == 'project: 1')


def test_keyword_problem():
    print('\n[_keyword_problem]')
    check('两词通过', A._keyword_problem('train station') is None)
    check('四词通过', A._keyword_problem('people subway phone night') is None)
    check('连字符通过', A._keyword_problem('city night aerial') is None)
    check('空词被拒', A._keyword_problem('') is not None)
    check('中文被拒', A._keyword_problem('城市 夜景') is not None)


def main():
    for fn in (test_real_scripts_pass, test_accepts_good, test_rejects_bad,
               test_no_search, test_summarize, test_apply_defaults,
               test_extract_yaml, test_keyword_problem):
        fn()
    print(f'\n{"=" * 56}')
    print(f'通过 {len(PASS)}  失败 {len(FAIL)}')
    if FAIL:
        print('\n失败项:')
        for f in FAIL:
            print(f'  - {f}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
