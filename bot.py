import os, json, time, re, asyncio, urllib.request, urllib.error, argparse, signal, subprocess, sys
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit
from collections import Counter, defaultdict
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
PIDFILE = Path(__file__).parent / "bot.pid"
LOGFILE = Path(__file__).parent / "bot.log"

def _pid_running(pid):
    try: os.kill(pid, 0); return True
    except: return False

def _status():
    if not PIDFILE.exists(): print("● stopped (no pid file)"); return False
    try: pid=int(PIDFILE.read_text().strip())
    except: print("● pid file corrupt"); return False
    if _pid_running(pid): print(f"● running  pid {pid}"); return True
    print(f"○ stale pid {pid} — not running"); return False

def _stop():
    if not PIDFILE.exists(): print("○ not running"); return
    try: pid=int(PIDFILE.read_text().strip())
    except: print("✘ pid file corrupt"); return
    if not _pid_running(pid): print(f"○ stale pid {pid}"); PIDFILE.unlink(missing_ok=True); return
    try: os.kill(pid, signal.SIGTERM); print(f"◐ stopping pid {pid} …")
    except Exception as e: print(f"✘ {e}"); return
    for _ in range(30):
        time.sleep(0.5)
        if not _pid_running(pid): print("● stopped"); PIDFILE.unlink(missing_ok=True); return
    print("✘ still running — try kill -9")

def _run_background(log_path):
    if PIDFILE.exists():
        try:
            pid=int(PIDFILE.read_text().strip())
            if _pid_running(pid): print(f"● already running  pid {pid}"); return
            PIDFILE.unlink(missing_ok=True)
        except: PIDFILE.unlink(missing_ok=True)
    log_path=Path(log_path)
    if not log_path.is_absolute(): log_path=Path(__file__).parent / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"▶ starting in background  log: {log_path}")
    with open(log_path, "ab") as lf:
        p=subprocess.Popen([sys.executable, __file__], stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    PIDFILE.write_text(str(p.pid))
    time.sleep(1)
    if _pid_running(p.pid): print(f"● running  pid {p.pid}  — logs: tail -f {log_path}")
    else: print(f"✘ failed to start — check {log_path}")
_early_parser=argparse.ArgumentParser(add_help=False)
_early_parser.add_argument("--background","-b", action="store_true")
_early_parser.add_argument("--stop", action="store_true")
_early_parser.add_argument("--status", action="store_true")
_early_parser.add_argument("--log", default=str(LOGFILE))
try: _early, _ = _early_parser.parse_known_args()
except: _early=None
if _early and (_early.status or _early.stop or _early.background):
    if _early.status: _status(); sys.exit(0)
    if _early.stop: _stop(); sys.exit(0)
    if _early.background:
        if not TOKEN: raise SystemExit("Missing DISCORD_TOKEN in .env")
        _run_background(_early.log); sys.exit(0)
if not TOKEN:
    raise SystemExit("Missing DISCORD_TOKEN in .env — set in discord-bot/.env")

def _norm(base):
    s=urlsplit(base.strip().strip("'\""))
    b=urlunsplit((s.scheme, s.netloc, s.path.rstrip("/"), "", "")).rstrip("/")
    return b or base.strip().strip("'\"").split("?")[0].split("#")[0].rstrip("/")

def _urls(base):
    b=_norm(base)
    if b.endswith("/v1/models"): return [b]
    if b.endswith("/v1"): return [b+"/models"]
    if b.endswith("/models"): return [b]
    return [b+"/v1/models", b+"/models"]

def _chat_urls(base):
    b=_norm(base)
    if b.endswith("/v1/chat/completions"): return [b]
    if b.endswith("/v1"): return [b+"/chat/completions"]
    if b.endswith("/chat/completions"): return [b]
    return [b+"/v1/chat/completions", b+"/chat/completions"]

def _human(n):
    if not isinstance(n,int) or n<=0: return "-"
    if n>=1_000_000: return f"{n/1_000_000:.1f}M".replace(".0M","M")
    if n>=1000: return f"{n/1000:.0f}k"
    return str(n)

def _owner(m):
    if m.get("owned_by"): return m["owned_by"]
    mid=m.get("id","")
    if "/" in mid: return mid.split("/")[0]
    if "-" in mid: return mid.split("-")[0]
    return "other"

def _caps(m):
    c=m.get("capabilities")
    if isinstance(c, list):
        low=[str(x).lower() for x in c]
        s=[]
        if any("vision" in x or "image" in x for x in low): s.append("V")
        if any("audio" in x for x in low): s.append("A")
        if any("tool" in x for x in low): s.append("T")
        if any("reason" in x for x in low): s.append("R")
        if any("search" in x for x in low): s.append("S")
        arch=m.get("architecture",{})
        if not s and arch.get("input_modalities") and "image" in arch["input_modalities"]: s.append("V")
        return "".join(s) or ("T" if "tools" in low else "-")
    if not c: c={}
    if not c and "context_length" not in m: return "-"
    s=[]
    if c.get("vision") or m.get("vision"): s.append("V")
    if c.get("audioInput"): s.append("A")
    if c.get("videoInput"): s.append("Vd")
    if c.get("tools") or m.get("tools"): s.append("T")
    if c.get("reasoning"): s.append("R")
    if c.get("search"): s.append("S")
    return "".join(s) if s else "-"

def _ctx(m):
    c=m.get("capabilities")
    v=c.get("contextWindow") if isinstance(c,dict) else None
    return v or m.get("context_length") or m.get("contextWindow") or 0

def _req(url, key=None, data=None, timeout=10):
    h={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36","Accept":"application/json"}
    if key: h["Authorization"]=f"Bearer {key}"
    if data is not None:
        h["Content-Type"]="application/json"
        d=json.dumps(data).encode()
    else: d=None
    r=urllib.request.Request(url, data=d, headers=h)
    t=time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw=resp.read().decode(errors="ignore")
            try: j=json.loads(raw)
            except: j={"_raw":raw[:500]}
            return j, raw, time.time()-t, resp.status, None
    except urllib.error.HTTPError as e:
        raw=e.read().decode(errors="ignore")[:800]
        try: j=json.loads(raw)
        except: j={"_raw":raw}
        return j, raw, time.time()-t, e.code, raw[:300]
    except Exception as e:
        return {"error":str(e)[:300]}, "", time.time()-t, 0, str(e)[:300]

def _fetch(base, key, timeout=10):
    errs=[]
    for u in _urls(base):
        j,raw,dt,code,err=_req(u,key,None,timeout)
        if code==200 and isinstance(j,dict) and ("data" in j or "models" in j):
            data=j.get("data") or j.get("models") or []
            if isinstance(data,dict): data=[{"id":k,**(v if isinstance(v,dict) else {})} for k,v in data.items()]
            return data, dt, code, u, None
        if code==200 and isinstance(j,list): return j,dt,code,u,None
        errs.append((u,code,err or str(j)[:120]))
    return None, dt, code, None, errs

def _probe_model(base, key, mid, timeout=10):
    for u in _chat_urls(base):
        j,raw,dt,code,err=_req(u,key,{"model":mid,"messages":[{"role":"user","content":"hi"}],"max_tokens":5},timeout)
        if code==200 and ("choices" in j or "_raw" in j and "choices" in raw): return "ok",dt,code,"ok",u
        if code in (401,403): return "auth",dt,code,(j.get("error",{}).get("message") if isinstance(j.get("error"),dict) else str(j.get("error","")))[:60] or err[:60],u
        if code>=400:
            msg=j.get("error",{}).get("message") if isinstance(j.get("error"),dict) else j.get("error") or err
            return "err",dt,code,str(msg)[:60],u
    return "err",dt,code,err[:60] if err else "failed",u

intents=discord.Intents.default()
client=discord.Client(intents=intents)
tree=app_commands.CommandTree(client)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user} — {len(tree.get_commands())} commands synced")

def _build_embeds(models, dt, ok_url, host, base, search, limit=25):
    for m in models:
        if "owned_by" not in m: m["owned_by"]=_owner(m)
    cnt=Counter(m["owned_by"] for m in models)
    e1=discord.Embed(title=f"● Live — {len(models)} models", color=0x2ecc71, description=f"`{host}` • `{ok_url}` • `{dt*1000:.0f}ms`")
    e1.add_field(name="Providers", value="\n".join(f"`{p:<14}` **{n}**" for p,n in cnt.most_common()) or "-", inline=False)
    if search: e1.add_field(name="Filter", value=f"`search={search}`", inline=True)
    e1.add_field(name="Base URL", value=f"`{base}`", inline=False)
    e1.set_footer(text="CAPS: V=vision A=audio T=tools R=reasoning S=search  ●=reasoning")
    rows=sorted(models, key=lambda x:(x["owned_by"],x["id"]))
    if search: rows=[r for r in rows if search.lower() in r["id"].lower()]
    total=len(rows)
    shown=rows[:limit]
    lines=["```","MODEL                            CTX    CAPS","─"*46]
    for m in shown:
        mid=m["id"]
        disp=mid if len(mid)<=28 else mid[:27]+"…"
        lines.append(f"{disp:<28} {_human(_ctx(m)):>6}  {_caps(m):<6}")
    if total>limit: lines.append(f"… +{total-limit} more (use search to filter)")
    lines.append("```")
    e1.description = e1.description + "\n" + "\n".join(lines)
    return [e1], cnt, rows

@tree.command(name="probe", description="Check any OpenAI-compatible provider")
@app_commands.describe(
    base_url="Base URL (e.g. https://api.openai.com/v1)",
    api_key="API key (sent ephemerally, not stored)",
    search="Filter model id (substring)",
    test="Live-test 1 model per provider",
    test_all="Live-test all models (uses quota)",
    limit="Max models to show (default 25)",
)
async def probe(
    interaction: discord.Interaction,
    base_url: str,
    api_key: str,
    search: str = None,
    test: bool = False,
    test_all: bool = False,
    limit: int = 25,
):
    await interaction.response.defer(ephemeral=True)
    host=urlparse(base_url).netloc or base_url
    try:
        models, dt, code, ok_url, errs = await asyncio.to_thread(_fetch, base_url, api_key, 10)
    except Exception as e:
        await interaction.followup.send(f"✘ error: `{e}`", ephemeral=True)
        return
    if models is None:
        desc="\n".join(f"`{u}` → **{c}** {e[:60]}" for u,c,e in errs)
        emb=discord.Embed(title="✘ Unreachable", color=0xe74c3c, description=f"`{host}` • `{base_url}`\n{desc}\n\nTips: base URL should end with `/v1`")
        await interaction.followup.send(embed=emb, ephemeral=True)
        return
    embeds, cnt, rows = _build_embeds(models, dt, ok_url, host, base_url, search, limit)
    await interaction.followup.send(embeds=embeds, ephemeral=True)
    if not (test or test_all): return
    await interaction.followup.send(f"⏳ Live testing **{len(cnt) if test and not test_all else len(rows)}** {'providers' if test and not test_all else 'models'} — `max_tokens=5`", ephemeral=True)
    # build targets
    by=defaultdict(list)
    for m in models: by[m["owned_by"]].append(m["id"])
    targets=[]
    if test_all:
        for prov, ids in by.items():
            for mid in ids:
                if search and search.lower() not in mid.lower(): continue
                targets.append((prov, mid))
    else:
        for prov, ids in by.items():
            pick=next((x for x in ids if not search or search.lower() in x.lower()), None)
            if pick: targets.append((prov, pick))
    async def _chk(t):
        prov,mid=t
        st,dt,c,msg,_ = await asyncio.to_thread(_probe_model, base_url, api_key, mid, 12)
        return prov,mid,st,int(dt*1000),c,msg
    results=await asyncio.gather(*[_chk(t) for t in targets])
    ok=[r for r in results if r[2]=="ok"]
    bad=[r for r in results if r[2]!="ok"]
    avg=sum(r[3] for r in results)//max(1,len(results))
    if test_all:
        desc=f"**{len(ok)} working** • **{len(bad)} failed** • `{avg}ms avg`\n\n"
        if ok: desc+="**✔ Working**\n" + "\n".join(f"`{r[1]}` `{r[3]}ms`" for r in sorted(ok, key=lambda x:x[1])[:20]) + ("\n…" if len(ok)>20 else "") + "\n"
        if bad: desc+="\n**✘ Failed**\n" + "\n".join(f"`{r[1]}` `{r[4]}` {r[5][:30]}" for r in sorted(bad, key=lambda x:x[1])[:15]) + ("\n…" if len(bad)>15 else "")
        emb=discord.Embed(title=f"── Summary ── {len(results)} total", color=0x2ecc71 if not bad else 0xf39c12, description=desc[:4096])
    else:
        emb=discord.Embed(title="Live Test", color=0x2ecc71 if not bad else 0xf39c12, description=f"`{len(ok)}/{len(results)} ok` • `{avg}ms avg`\n" + "\n".join(f"{'✔' if r[2]=='ok' else '✘'} `{r[1][:36]}` `{r[3]}ms` {r[5][:30] if r[2]!='ok' else 'ok'}" for r in results))
    # JSON file
    payload={
        "provider": host, "base_url": base_url,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": {"total": len(results), "working": len(ok), "failed": len(bad)},
        "working": sorted([r[1] for r in ok]),
        "failed": [{"id":r[1],"status":r[2],"code":r[4],"error":r[5]} for r in bad],
        "results": [{"id":r[1],"provider":r[0],"status":r[2],"code":r[4],"latency_ms":r[3]} for r in results],
    }
    import io
    buf=io.BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode())
    file=discord.File(buf, filename=f"probe-result-{host}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    await interaction.followup.send(embed=emb, file=file, ephemeral=True)

client.run(TOKEN)
