"""端到端回归（手动运行）：长命令执行期间心跳不被饿死。

复现 2026-11-23 家用机掉线根因：旧 device.py 在 async 主循环里同步调用 server.dispatch，
慢命令冻结 event loop → heartbeat task 与 websocket keepalive pong 发不出 → 服务端判离线。

运行：python3 prototype/tests/e2e_heartbeat_regression.py
预期：HEARTBEATS_DURING_4S_COMMAND >= 8 且 E2E_HB_ALIVE_PASSED
（用未修复的 device.py 跑同一脚本会得到 0 个心跳 + E2E_HB_STARVED）。
"""
"""决定性判据：长命令执行期间，服务端收到的 heartbeat 帧数量。

device 心跳 task 每 0.2s 发一次（patch 后）。长命令(sleep 4s)执行期间：
- 修复版：dispatch 在线程池，event loop 空闲 → 心跳 task 正常发 → 服务端收到 ~15+ 个 heartbeat
- 基线：同步 dispatch 冻结 event loop 4s → 心跳 task 停摆 → 服务端几乎收不到 heartbeat

这精确对应"心跳被饿死→服务端判 1011 离线"的真实机制。
"""
import asyncio, sys, os, tempfile, time
from pathlib import Path
_ROOT=os.environ.get('E2E_CODE_ROOT',str(Path(__file__).resolve().parents[1] / 'ws_remote'))
sys.path.insert(0,_ROOT); sys.path.insert(0,os.environ.get('E2E_PROTO_ROOT',_ROOT))
import server as S, user_store as US, device as D
from websockets.asyncio.server import serve as ws_serve

tmp=tempfile.mkdtemp(); db=str(Path(tmp)/'hub.db'); US.init_schema(db)
import sqlite3
with sqlite3.connect(db) as c:
    c.executescript(
      "CREATE TABLE IF NOT EXISTS devices(device_id TEXT PRIMARY KEY,display_name TEXT,platform TEXT,key_hash TEXT,status TEXT,info_json TEXT,last_seen TEXT,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT,owner_id INTEGER NOT NULL DEFAULT 0);"
      "CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,device_id TEXT,tool TEXT,args_json TEXT,state TEXT,result_json TEXT,error TEXT,created_at TEXT,finished_at TEXT,owner_id INTEGER NOT NULL DEFAULT 0);"
      "CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,actor TEXT,action TEXT,target TEXT,ip TEXT,detail TEXT,result TEXT);")
admin,_=US.create_user(db,'a@x.com','Str0ng!Pass2026','admin')
hub=S.Hub(db,control_token='t')
hb_count={'n':0}

# 计数服务端收到的 heartbeat
async def counted_handler(ws):
    did=None
    try:
        raw=await asyncio.wait_for(ws.recv(),timeout=15)
        auth=json.loads(raw) if False else __import__('json').loads(raw)
        dev=hub.device_by_key(auth.get('device_id',''),auth.get('key',''))
        if not dev: return
        did=dev['device_id']
        hub.conns[did]=S.DeviceConn(device_id=did,ws=ws,platform=auth.get('platform',''),info=auth.get('info') or {},last_seen=time.time())
        await ws.send(__import__('json').dumps({'type':'auth_ack','ok':True,'device_id':did}))
        async for raw in ws:
            m=__import__('json').loads(raw)
            if m.get('type')=='heartbeat':
                hb_count['n']+=1
                hub.conns[did].last_seen=time.time()
            elif m.get('type')=='result':
                fut=hub.pending.get(m.get('task_id'))
                if fut and not fut.done():
                    fut.set_result({k:v for k,v in m.items() if k not in ('type','task_id')})
    finally:
        if did in hub.conns: del hub.conns[did]

async def main():
    dev_id,key=hub.create_device('e2e','t',admin['id'])
    cfg={'server_url':'ws://127.0.0.1:18867','device_id':dev_id,'key':key,'platform':'t','allowlist':['python3','sleep']}
    st=D.DeviceState(config=cfg,config_path=str(Path(tmp)/'d.json'),last_change=time.time())
    async with ws_serve(counted_handler,'127.0.0.1',18867):
        # patch 心跳间隔 0.2s（device 源码里 heartbeat 的 asyncio.sleep(20)）
        src_mod = sys.modules.get('device')
        # 直接改源码常量不可行；用 monkeypatch asyncio.sleep 太粗暴。改为替换 run 里的 heartbeat：
        # 简单做法：临时把模块级一个可调常量注入——若没有则 patch D.DeviceState 无关。
        # 这里用最短路径：patch time.sleep 不生效（async）。直接复制 run 源码替换 20→0.2 后 exec。
        import inspect, textwrap
        run_src = inspect.getsource(D.run)
        run_src = run_src.replace('await asyncio.sleep(20)', 'await asyncio.sleep(0.2)')
        # 去掉装饰器缩进后用 exec 注入模块命名空间
        ns = dict(D.__dict__); ns['asyncio']=__import__('asyncio')
        exec(textwrap.dedent(run_src), ns)
        run_fast = ns['run']
        dt=asyncio.create_task(run_fast(st,once=False))
        for _ in range(50):
            if dev_id in hub.conns: break
            await asyncio.sleep(0.1)
        assert dev_id in hub.conns, '未上线'
        base=hb_count['n']
        # 长命令 4s
        task=asyncio.create_task(hub.dispatch_task(dev_id,'exec',{'command':['sleep','4'],'timeout':10},admin['id']))
        await task
        during=hb_count['n']-base
        print('HEARTBEATS_DURING_4S_COMMAND=%d' % during, flush=True)
        if during >= 8:
            print('E2E_HB_ALIVE_PASSED 心跳在长命令期间持续发送', flush=True)
        else:
            print('E2E_HB_STARVED 心跳被长命令饿死（会掉线）', flush=True)
        st.shutdown=True; dt.cancel()
        try: await dt
        except asyncio.CancelledError: pass

asyncio.run(main())
