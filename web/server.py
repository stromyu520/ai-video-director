"""server.py — FastAPI 服务：路由 + SSE + 静态托管

启动：
    video-env/bin/python -m web.server          # 或 uvicorn web.server:app

必须用 video-env 的 python（3.11，有 tomllib；系统 python3 是 3.10 没有）。
必须单进程：JobManager 的任务表在内存里，--workers 2 会复制一份，
两个 worker 各跑各的队列，前端会看到一半任务查不到。

安全边界（用户已确认）：仅绑定 127.0.0.1、无鉴权。
密钥只以掩码回显；空提交 = 不修改。
"""
import asyncio
import json
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

# 允许 `python -m web.server` 从仓库根启动，也允许从别处启动
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import Body, FastAPI, HTTPException, Query, Request, UploadFile    # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse    # noqa: E402
from fastapi.staticfiles import StaticFiles                                    # noqa: E402

from project_paths import COSYVOICE_MODEL                                      # noqa: E402
from web import config, projects                                               # noqa: E402
from web.fsutil import safe_join, sanitize_name                                # noqa: E402
from web.jobs import JobManager                                                # noqa: E402
from web.pipelines import PIPELINES, validate_params                           # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / 'static'
MAX_UPLOAD_MB = 2048
CHUNK = 1 << 20
VOICES_TTL = 3600.0          # 线上音色清单的缓存寿命（这份表几乎不变）
# 允许的上传落点（相对项目目录）。空串 = 根目录（放原片）。加目录前先确认
# 管线的素材发现逻辑真的会扫它，否则传上去就是死文件，见 upload() 的 docstring。
#
# 页面上的「上传素材」现在一律传 '素材'（图片和短视频同层），不再按扩展名分流 ——
# 但白名单里保留 图片/短片，它们仍是**管线自己写**的落点（Pexels 自动下载），
# 手工接口也还有人用。目录约定的真值源是 pipeline_config.MANUAL_DIRS。
UPLOAD_DIRS = ('', '素材', '图片', '短片')

mgr = JobManager(PIPELINES)


@asynccontextmanager
async def lifespan(app):
    if os.environ.get('WEB_CONCURRENCY'):
        # 多 worker 会把内存里的任务表复制成多份，前端随机看到一半任务
        raise RuntimeError(
            '禁止多 worker 运行（WEB_CONCURRENCY 已设置）。'
            'JobManager 的状态在进程内存里，必须单进程。')
    for p in config.self_check():
        print(f'⚠️  {p}', flush=True)
    await mgr.start()
    print(f'✅ 就绪：{len(mgr.jobs)} 个历史任务，'
          f'{len(PIPELINES)} 条管线', flush=True)
    yield
    await mgr.stop()


app = FastAPI(title='智能剪辑', lifespan=lifespan)


def _err(code, msg):
    return JSONResponse({'error': msg}, status_code=code)


# ============================ 静态 ============================

@app.get('/')
async def index():
    f = STATIC_DIR / 'index.html'
    if not f.exists():
        return _err(500, 'web/static/index.html 缺失')
    return FileResponse(f)


if STATIC_DIR.is_dir():
    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


# ============================ 健康与配置 ============================

@app.get('/api/health')
async def health():
    cfg = config.load()
    ffmpeg = shutil.which('ffmpeg')
    gpu = None
    try:
        r = await asyncio.to_thread(
            lambda: __import__('subprocess').run(
                ['nvidia-smi', '--query-gpu=name,memory.used,memory.total',
                 '--format=csv,noheader'], capture_output=True, text=True,
                timeout=15))
        gpu = r.stdout.strip() or None
    except Exception:                                        # noqa: BLE001
        pass
    return {
        'ok': True,
        'python': sys.version.split()[0],
        'ffmpeg': ffmpeg or None,
        'gpu': gpu,
        'llm_ready': bool(cfg['llm']['api_key']),
        'pexels_ready': bool(cfg['pexels']['api_key']),
        'running': sum(1 for j in mgr.jobs.values() if not j.is_terminal),
        'port': 8000,
    }


@app.get('/api/config')
async def get_config():
    return config.public_view()


@app.post('/api/config')
async def set_config(patch: dict = Body(...)):
    """写配置。

    密钥语义：**空串 = 不修改**（前端只回显掩码，不可能把原值发回来）。
    要清空密钥得显式传 null。
    """
    clean = {}
    for section in ('llm', 'pexels', 'defaults', 'runner'):
        vals = (patch or {}).get(section)
        if not isinstance(vals, dict):
            continue
        out = {}
        for k, v in vals.items():
            if k in ('api_key',) and (v is None or v == ''):
                continue          # 空 = 保持原值
            out[k] = v
        if out:
            clean[section] = out
    if clean:
        config.save(clean)
    return config.public_view()


@app.post('/api/config/test-llm')
async def test_llm():
    """真发一次最小请求。注意 max_tokens 要留够推理预算：
    deepseek-v4-flash 即使关掉思考也会吐推理 token，16 个 token 会被吃光，
    表现为 HTTP 200 但 content 为空。"""
    import requests
    cfg = config.load()['llm']
    if not cfg['api_key']:
        return _err(400, '未配置 LLM key')
    payload = {
        'model': cfg['model'],
        'messages': [{'role': 'user', 'content': '回复两个字：通了'}],
        'max_tokens': 256,
        'thinking': {'type': 'disabled'},
    }
    try:
        r = await asyncio.to_thread(lambda: requests.post(
            cfg['base_url'], json=payload, timeout=60,
            headers={'Authorization': f'Bearer {cfg["api_key"]}'}))
    except Exception as e:                                   # noqa: BLE001
        return _err(502, f'连不上: {e}')
    if r.status_code != 200:
        return _err(502, f'HTTP {r.status_code}: {r.text[:200]}')
    try:
        content = r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:                                   # noqa: BLE001
        return _err(502, f'响应格式异常: {e}')
    if not content:
        return _err(502, '连接正常但返回为空（多半是 max_tokens 被推理 token 吃光）')
    return {'ok': True, 'model': cfg['model'], 'reply': content[:60]}


# ============================ 项目 ============================

@app.get('/api/projects')
async def list_projects(category: str = Query(None)):
    try:
        return {'projects': await asyncio.to_thread(
            projects.list_projects, category)}
    except ValueError as e:
        return _err(400, str(e))


@app.post('/api/projects')
async def create_project(body: dict = Body(...)):
    try:
        s = await asyncio.to_thread(
            projects.create, body.get('category'), body.get('name'),
            body.get('kind'))
    except ValueError as e:
        return _err(400, str(e))
    return s


@app.get('/api/projects/{category}/{name}')
async def get_project(category: str, name: str):
    try:
        s = await asyncio.to_thread(projects.summarize, category, name)
    except ValueError as e:
        return _err(400, str(e))
    if not s:
        return _err(404, '项目不存在')
    return s


@app.delete('/api/projects/{category}/{name}')
async def delete_project(category: str, name: str):
    """删项目 = 删掉整个 `分类/项目名/` 目录（成片、素材、分析结果一起走）。

    这是整个页面上唯一**不可撤销**的操作，所以两道闸：
      · 有任务在跑就不给删 —— 渲染器正在往这个目录里写，边写边删会留下
        半截文件，而且失败信息会指着一个已经不存在的路径；
      · 前端必须二次确认（确认框里把「整个文件夹」写明白），
        删之前还要求用户把项目名敲一遍。

    路径安全由 projects.remove 兜底：它走 safe_join，且拒绝 '.' / '..'
    这种会解析成**分类目录本身**的名字 —— 少了那层，删「新闻/.」就等于
    把整个分类连锅端。
    """
    running = [j for j in mgr.jobs.values()
               if j.category == category and j.project == name
               and not j.is_terminal]
    if running:
        return _err(409, f'还有任务在跑（{running[0].id}），先取消再删')
    try:
        await asyncio.to_thread(projects.remove, category, name)
    except ValueError as e:
        return _err(400, str(e))
    except FileNotFoundError:
        return _err(404, '项目不存在')
    # 任务记录跟着删：留着的话列表里会永远挂着一个「项目已不存在」的卡片
    for jid in [j.id for j in list(mgr.jobs.values())
                if j.category == category and j.project == name]:
        mgr.jobs.pop(jid, None)
    return {'ok': True, 'removed': f'{category}/{name}'}


@app.get('/api/kinds')
async def list_kinds():
    """三种「视频类型」→ 管线 + 模式的映射，给前端渲染类型选择器。
    顺带把分类也带回去 —— 分类只有 projects 一处定义，前端再抄一份就会两边不一致。
    用 discover_categories() 而不是 CATEGORIES：前者会把磁盘上已有的分类
    （其他/ 二创/ 这种早期建的）一并列出，否则新建下拉里选不到它们。"""
    return {'kinds': [dict(id=k, **v) for k, v in projects.KINDS.items()],
            'categories': list(projects.discover_categories())}


@app.post('/api/projects/{category}/{name}/kind')
async def set_kind(category: str, name: str, body: dict = Body(...)):
    """改项目的视频类型。只写 project.json，不动任何已有产物。"""
    try:
        pdir = await asyncio.to_thread(projects.project_dir, category, name)
        await asyncio.to_thread(projects.write_kind, pdir, body.get('kind'))
    except ValueError as e:
        return _err(400, str(e))
    except FileNotFoundError:
        return _err(404, '项目不存在')
    return await asyncio.to_thread(projects.summarize, category, name)


@app.post('/api/projects/{category}/{name}/settings')
async def set_settings(category: str, name: str, body: dict = Body(...)):
    """项目上的开关，目前只有「自动搜索」。只写 project.json，不动任何产物。

    只处理 body 里**明确出现**的键：空 body 或没带 search 时什么都不写 ——
    否则前端每次顺手 POST 一下就会把开关重置成默认值。
    """
    try:
        pdir = await asyncio.to_thread(projects.project_dir, category, name)
        if 'search' in (body or {}):
            await asyncio.to_thread(projects.write_search, pdir,
                                    bool(body.get('search')))
        summary = await asyncio.to_thread(projects.summarize, category, name)
    except ValueError as e:
        return _err(400, str(e))
    except FileNotFoundError:
        return _err(404, '项目不存在')
    return summary


@app.get('/api/projects/{category}/{name}/state')
async def project_state(category: str, name: str):
    """工作台一次调用拿全：概要 + 产物 + 场景 + 文件 + 该项目最近的任务。

    刻意做成**一个**聚合路由：工作台要同时渲染五六块，分开请求会出现
    「成片已出但场景还是旧的」这类半截界面，而且切项目时容易串响应。
    """
    try:
        pdir = await asyncio.to_thread(projects.project_dir, category, name)
        summary = await asyncio.to_thread(projects.summarize, category, name)
        files = await asyncio.to_thread(projects.list_files, pdir)
        scenes = await asyncio.to_thread(projects.parse_scenes, pdir)
        # 声明片长 vs 实际片长（要 ffprobe，必须丢线程里，别卡事件循环）
        dur = await asyncio.to_thread(projects.duration_report, pdir)
    except ValueError as e:
        return _err(400, str(e))
    except FileNotFoundError:
        return _err(404, '项目不存在')

    # 该项目最近的任务（含正在跑的），工作台据此决定显示进度还是表单
    mine = [j for j in mgr.jobs.values()
            if j.category == category and j.project == name]
    mine.sort(key=lambda j: j.created_at, reverse=True)

    def url(fn):
        from urllib.parse import quote
        return (f'/media/{quote(category)}/{quote(name)}/'
                f'{quote(fn)}')

    for f in summary['artifacts']:
        f['url'] = url(f['name'])
    return {
        'project': summary,
        'files': files,
        'scenes': scenes,
        'duration': dur,
        'jobs': [mgr.snapshot(j.id) for j in mine[:5]],
        'kinds': [dict(id=k, **v) for k, v in projects.KINDS.items()],
    }


@app.get('/api/voices')
async def list_voices(force: bool = False):
    """音色清单：本地 CosyVoice + 线上 Edge / OpenAI 兼容。

    本地那份要起一个**短命子进程**去读 spk2info.pt（反序列化要 torch，
    而服务器进程绝不能 import torch —— 本机内存已满）。所以结果按该文件的
    mtime 缓存到磁盘，重启也不用再付一次。**别用 voice_cli.py 列表**：
    它在分发前无条件加载整个模型，实测 21.0s。
    """
    import tts_backends as tb
    tts = config.load()['tts']
    cache_dir = Path(__file__).resolve().parent / 'cache'
    cache = cache_dir / 'voices.json'

    spk = COSYVOICE_MODEL / 'spk2info.pt'
    try:
        stamp = f'{spk.stat().st_mtime:.0f}:{spk.stat().st_size}'
    except OSError:
        stamp = ''

    local = None
    cached = {}
    try:
        cached = json.loads(cache.read_text(encoding='utf-8'))
    except Exception:                                        # noqa: BLE001
        cached = {}
    if not force and cached.get('stamp') == stamp and cached.get('local'):
        local = cached['local']

    if local is None:
        local = await asyncio.to_thread(tb.local_voices)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(
                {'stamp': stamp, 'local': local, 'at': time.time()},
                ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass            # 缓存写不了不影响功能，下次再读一遍而已

    backends = []
    for bid, bcfg in (tts.get('backends') or {}).items():
        if not isinstance(bcfg, dict):
            continue
        ready = True
        if bid == 'openai':
            ready = bool(bcfg.get('base_url') and bcfg.get('api_key'))
        backends.append({
            'id': bid,
            'label': bcfg.get('label') or bid,
            'enabled': bool(bcfg.get('enabled', True)),
            'ready': ready,
            'default_voice': await asyncio.to_thread(tb.default_voice, bid),
        })

    # 线上清单也缓存：每次打开工作台都去问一次 edge 要 1s，而这份表几乎不变。
    # 本地那份按 spk2info.pt 的 mtime 失效，这份按时间失效 —— 两把不同的钥匙。
    online = []
    if any(b['enabled'] for b in backends):
        fresh = (not force and cached.get('online')
                 and time.time() - cached.get('online_at', 0) < VOICES_TTL)
        if fresh:
            online = cached['online']
        else:
            online = await asyncio.to_thread(tb.online_voices, None)
            if online:
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(
                        {**cached, 'online': online, 'online_at': time.time(),
                         'stamp': stamp, 'local': local},
                        ensure_ascii=False), encoding='utf-8')
                except OSError:
                    pass

    return {
        'local': [{'id': v, 'name': v, 'provider': 'cosyvoice',
                   'label': v} for v in local],
        'online': online,
        'backends': backends,
        'active': tts.get('active', 'cosyvoice'),
    }


@app.get('/api/projects/{category}/{name}/file')
async def get_project_file(category: str, name: str, f: str = Query(...)):
    """读项目里的文本产物（director.yaml / remix.yaml / analysis.json）。"""
    try:
        pdir = await asyncio.to_thread(projects.project_dir, category, name)
        fp = safe_join(pdir, f)
    except ValueError as e:
        return _err(400, str(e))
    except FileNotFoundError:
        return _err(404, '项目不存在')
    if not fp.is_file():
        return _err(404, f'文件不存在: {f}')
    if fp.suffix.lower() in ('.mp4', '.mov', '.mkv', '.webm'):
        return _err(400, '视频请走 /media 路由')
    text = await asyncio.to_thread(
        lambda: fp.read_text(encoding='utf-8', errors='replace'))
    return {'name': f, 'text': text[:400_000]}


# ============================ 任务 ============================

@app.get('/api/jobs')
async def list_jobs(limit: int = 50):
    js = sorted(mgr.jobs.values(), key=lambda j: j.created_at, reverse=True)
    return {'jobs': [mgr.snapshot(j.id) for j in js[:max(1, min(limit, 500))]]}


@app.get('/api/jobs/{job_id}')
async def get_job(job_id: str):
    snap = mgr.snapshot(job_id)
    if not snap:
        return _err(404, '任务不存在')
    return snap


@app.post('/api/projects/{category}/{name}/jobs')
async def submit_job(category: str, name: str, body: dict = Body(...)):
    try:
        pdir = await asyncio.to_thread(projects.project_dir, category, name)
    except FileNotFoundError:
        return _err(404, '项目不存在')
    except ValueError as e:
        return _err(400, str(e))

    pipeline = (body or {}).get('pipeline') or 'director'
    params = dict((body or {}).get('params') or {})

    # 让 validate 知道 yaml 是否已存在（前端不必自己判断）
    params['director_yaml_exists'] = (pdir / 'director.yaml').is_file()
    # 同上：关了自动搜索时 validate 要能看出「一个素材都没有」并当场拒绝，
    # 别等智能体跑起来才报错。扫盘放线程里，别卡住事件循环。
    params['local_material_count'] = await asyncio.to_thread(
        projects.count_materials, pdir)
    errs = validate_params(pipeline, params)
    if errs:
        return _err(400, '；'.join(errs))

    busy = mgr.is_work_dir_busy(pdir)
    if busy:
        return _err(409, f'该项目已有任务在跑（{busy}），等它结束再提交')

    job = mgr.submit(pipeline, category=category, project=name,
                     work_dir=pdir, params=params)
    return mgr.snapshot(job.id)


@app.post('/api/jobs/{job_id}/cancel')
async def cancel_job(job_id: str):
    if job_id not in mgr.jobs:
        return _err(404, '任务不存在')
    ok = await mgr.cancel(job_id)
    return {'ok': ok, 'job': mgr.snapshot(job_id)}


@app.post('/api/jobs/{job_id}/resume')
async def resume_job(job_id: str, body: dict = Body(default={})):
    if job_id not in mgr.jobs:
        return _err(404, '任务不存在')
    if not mgr.resume(job_id, body or {}):
        return _err(409, '该任务当前没有等待中的确认点')
    return {'ok': True}


# ============================ SSE ============================

def _sse(ev):
    return (f'id: {ev["seq"]}\n'
            f'event: {ev.get("type", "message")}\n'
            f'data: {json.dumps(ev.get("data"), ensure_ascii=False)}\n\n')


@app.get('/api/jobs/{job_id}/events')
async def job_events(job_id: str, request: Request):
    if job_id not in mgr.jobs:
        return _err(404, '任务不存在')
    last_id = request.headers.get('last-event-id') or '0'
    try:
        after = int(last_id)
    except ValueError:
        after = 0

    async def stream():
        q = mgr.hub.subscribe(job_id)
        try:
            # 1) 重放补发的日志（history 只含 log 事件，见其 docstring）
            for ev in mgr.history(job_id, after=after):
                yield _sse(ev)
            # 2) state/progress 是快照不是增量，重放无法覆盖 —— 立刻补一份当前值，
            #    否则重连的客户端会一直显示断线前的旧状态。
            snap = mgr.snapshot(job_id)
            if snap:
                yield _sse({'seq': mgr.jobs[job_id]._seq, 'type': 'state',
                            'data': snap})
                yield _sse({'seq': mgr.jobs[job_id]._seq, 'type': 'progress',
                            'data': {'progress': snap.get('progress', 0),
                                     'phase': snap.get('phase', ''),
                                     'step': snap.get('step', ''),
                                     'heartbeat': True}})
            # 3) 实时。15s 无事件发注释行保活（代理与浏览器会掐掉静默连接）
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), 15.0)
                except asyncio.TimeoutError:
                    yield ': ping\n\n'
                    continue
                yield _sse(ev)
        finally:
            mgr.hub.unsubscribe(job_id, q)

    return StreamingResponse(stream(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',        # 万一以后挂在 nginx 后面
        'Connection': 'keep-alive',
    })


# ============================ 媒体 ============================

@app.get('/media/{category}/{name}/{filename:path}')
async def media(category: str, name: str, filename: str):
    """成片/关键帧预览。FileResponse 原生支持 Range，前端可直接拖进度条。"""
    try:
        pdir = await asyncio.to_thread(projects.project_dir, category, name)
        fp = safe_join(pdir, filename)
    except ValueError as e:
        return _err(400, str(e))
    except FileNotFoundError:
        return _err(404, '项目不存在')
    if not fp.is_file():
        return _err(404, '文件不存在')
    return FileResponse(fp)


# ============================ 上传（阶段5 用，先占位可用） ============================

@app.post('/api/projects/{category}/{name}/upload')
async def upload(category: str, name: str, file: UploadFile, dir: str = ''):
    """上传一个文件到项目里。

    `dir` 决定落点，因为管线的两套发现逻辑扫的目录不一样：
      - ''   根目录 —— **原片**放这里（解说/二创用，`projects._find_source_video`
             和前端 params.source 都按根目录找）
      - 素材/ —— 视频素材，`video_make_ffmpeg.discover_manual_videos` 和
             `director_agent.scan_materials` 都只在这里找视频
      - 图片/ —— 图片素材，`scan_materials` 扫这里 + 根目录
    早先一律写根目录，结果**上传的视频是死文件**：没有任何环节会发现它。
    """
    try:
        pdir = await asyncio.to_thread(projects.project_dir, category, name)
    except FileNotFoundError:
        return _err(404, '项目不存在')

    sub = (dir or '').strip().strip('/')
    if sub not in UPLOAD_DIRS:
        return _err(400, f'不支持的上传目录: {dir}（可选 {"、".join(UPLOAD_DIRS)}）')
    target = safe_join(pdir, sub) if sub else pdir
    target.mkdir(parents=True, exist_ok=True)

    fname = sanitize_name(Path(file.filename or 'upload.bin').name)
    cap = MAX_UPLOAD_MB << 20
    dest = safe_join(target, fname)
    part = dest.with_suffix(dest.suffix + '.part')
    written = 0
    try:
        with open(part, 'wb') as out:
            while chunk := await file.read(CHUNK):
                written += len(chunk)
                if written > cap:
                    raise HTTPException(413, f'超过 {MAX_UPLOAD_MB}MB 上限')
                await asyncio.to_thread(out.write, chunk)
        await asyncio.to_thread(os.replace, part, dest)
    except HTTPException:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:                                   # noqa: BLE001
        part.unlink(missing_ok=True)
        return _err(500, f'写入失败: {e}')
    return {'name': fname, 'size': written, 'path': str(dest)}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8000, log_level='info')
