#!/usr/bin/env python3
import argparse, json, os, sys, time, urllib.request, urllib.error, concurrent.futures, re
from urllib.parse import urlparse
from collections import Counter

C={"r":"\033[31m","g":"\033[32m","y":"\033[33m","b":"\033[34m","m":"\033[35m","c":"\033[36m","d":"\033[90m","B":"\033[1m","R":"\033[0m"}
def col(s,k,e=True): return f"{C[k]}{s}{C['R']}" if e and sys.stdout.isatty() else s
def human(n):
    if not isinstance(n,int) or n<=0: return "-"
    if n>=1_000_000: return f"{n/1_000_000:.1f}M".replace(".0M","M")
    if n>=1000: return f"{n/1000:.0f}k"
    return str(n)

def _norm(base):
    from urllib.parse import urlsplit, urlunsplit
    s=urlsplit(base.strip().strip("'\""))
    b=urlunsplit((s.scheme, s.netloc, s.path.rstrip("/"), "", "")).rstrip("/")
    return b or base.strip().strip("'\"").split("?")[0].split("#")[0].rstrip("/")

def urls(base):
    b=_norm(base)
    if b.endswith("/v1/models"): return [b]
    if b.endswith("/v1"): return [b+"/models"]
    if b.endswith("/models"): return [b]
    return [b+"/v1/models", b+"/models"]

def chat_urls(base):
    b=_norm(base)
    if b.endswith("/v1/chat/completions"): return [b]
    if b.endswith("/v1"): return [b+"/chat/completions"]
    if b.endswith("/chat/completions"): return [b]
    return [b+"/v1/chat/completions", b+"/chat/completions"]

def req(url, key=None, data=None, timeout=10):
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

def fetch_models(base, key, timeout):
    errs=[]
    for u in urls(base):
        j, raw, dt, code, err = req(u, key, None, timeout)
        if code==200 and isinstance(j,dict) and ("data" in j or "models" in j):
            data=j.get("data") or j.get("models") or []
            if isinstance(data,dict): data=[{"id":k, **(v if isinstance(v,dict) else {})} for k,v in data.items()]
            return data, dt, code, u, None
        if code==200 and isinstance(j,list):
            return j, dt, code, u, None
        errs.append((u, code, err or str(j)[:120]))
    return None, dt, code, None, errs

def probe_model(base, key, mid, timeout):
    for u in chat_urls(base):
        j, raw, dt, code, err = req(u, key, {"model":mid,"messages":[{"role":"user","content":"hi"}],"max_tokens":5}, timeout)
        if code==200 and ("choices" in j or "_raw" in j and "choices" in raw):
            txt=""
            try: txt=j["choices"][0].get("message",{}).get("content","") or j["choices"][0].get("text","") or "ok"
            except: txt="ok"
            return "ok", dt, code, txt[:60], u
        if code in (401,403): return "auth", dt, code, (j.get("error",{}).get("message") if isinstance(j.get("error"),dict) else str(j.get("error","")) )[:60] or err[:60], u
        if code>=400:
            msg=j.get("error",{}).get("message") if isinstance(j.get("error"),dict) else j.get("error") or err
            return "err", dt, code, str(msg)[:60], u
    return "err", dt, code, err[:60] if err else "failed", u

def owner_of(m):
    if m.get("owned_by") and m["owned_by"]!="": return m["owned_by"]
    mid=m.get("id","")
    if "/" in mid: return mid.split("/")[0]
    if ":" in mid: return mid.split(":")[0]
    if "-" in mid: return mid.split("-")[0]
    return "other"

def caps(m):
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
        if not s and arch.get("input_modalities"):
            if "image" in arch["input_modalities"]: s.append("V")
        return "".join(s) or ("T" if "tools" in low else "-")
    if not c: c={}
    if not c and "context_length" not in m and "contextLength" not in m: return "-"
    s=[]
    if c.get("vision") or m.get("vision"): s.append("V")
    if c.get("pdf"): s.append("P")
    if c.get("audioInput"): s.append("A")
    if c.get("videoInput"): s.append("Vd")
    if c.get("tools") or m.get("tools"): s.append("T")
    if c.get("reasoning"): s.append("R")
    if c.get("search"): s.append("S")
    return "".join(s) if s else ("T" if m.get("tools") else "-")

def ctx_of(m):
    c=m.get("capabilities")
    if isinstance(c, dict): v=c.get("contextWindow")
    else: v=None
    return v or m.get("context_length") or m.get("contextWindow") or m.get("context_window") or 0

def out_of(m):
    c=m.get("capabilities")
    if isinstance(c, dict): v=c.get("maxOutput")
    else: v=None
    return v or m.get("max_completion_tokens") or m.get("maxOutput") or m.get("top_provider",{}).get("max_completion_tokens") or 0

def main():
    ap=argparse.ArgumentParser(description="Probe any OpenAI-compatible provider — just Base URL + API Key. Lists models in a clean, readable format.", epilog="examples:\n  probe.py https://api.openai.com/v1 sk-xxx\n  probe.py -u https://api.anthropic.com -k sk-xxx --test\n  probe.py --base-url http://localhost:11434/v1 --api-key ollama --search gpt --test", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base_url", nargs="?", help="provider base URL (e.g. https://api.openai.com/v1)")
    ap.add_argument("api_key", nargs="?", help="API key")
    ap.add_argument("-u","--base-url", dest="base_url2", help="base URL")
    ap.add_argument("-k","--api-key", dest="api_key2", help="API key")
    ap.add_argument("-p","--provider", help="filter by owner/provider")
    ap.add_argument("-s","--search", help="filter model id (substring)")
    ap.add_argument("--sort", choices=["provider","ctx","name"], default="provider", help="sort by")
    ap.add_argument("--test", action="store_true", help="live-test 1 model per provider (chat, max_tokens=5)")
    ap.add_argument("--test-all", action="store_true", help="live-test all models (uses quota)")
    ap.add_argument("--limit", type=int, default=0, help="limit displayed models (0=all)")
    ap.add_argument("--timeout", type=int, default=10, help="timeout in seconds")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--json", action="store_true", help="output raw JSON")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--save", nargs="?", const="auto", metavar="FILE", help="save test results to JSON (auto name if no value)")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verify (not implemented, stdlib verifies)")
    a=ap.parse_args()
    use=not a.no_color and sys.stdout.isatty()
    base=(a.base_url2 or a.base_url or os.environ.get("PROVIDER_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").strip().strip("'\"")
    key=(a.api_key2 or a.api_key or os.environ.get("PROVIDER_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip().strip("'\"")
    if not base and use:
        print(col("  ─ paste Base URL & API Key separately ─","d",use))
        print(col("  example: https://api.openai.com/v1  |  http://localhost:11434/v1","d",use))
        print()
        try: base=input(col("  ➜ Base URL: ","c",use)).strip().strip("'\"")
        except: pass
        if base and not key:
            try: key=input(col("  ➜ API Key : ","c",use)).strip().strip("'\"")
            except: pass
    elif not base:
        try: base=input("Base URL: ").strip().strip("'\"")
        except: pass
    if not base:
        ap.print_help(); print("\nerror: base URL required"); sys.exit(2)
    if not key:
        print(col("  ! no API key — listing without auth, --test requires a key","y",use))
    b=lambda s: col(s,"B",use)
    d=lambda s: col(s,"d",use)
    g=lambda s: col(s,"g",use)
    r=lambda s: col(s,"r",use)
    y=lambda s: col(s,"y",use)
    cc=lambda s: col(s,"c",use)
    m=lambda s: col(s,"m",use)

    parsed=urlparse(base)
    host=parsed.netloc or base
    print()
    print(b(f"  ━━ Provider Check ━━") + d(f"  {time.strftime('%Y-%m-%d %H:%M')}"))
    print(d("  "+"─"*54))
    print(f"  {d('▸')} {b(host)}  {d(base)}")
    if key: print(f"  {d('▸')} {d('key')} {key[:8]}…{key[-4:] if len(key)>12 else ''}  {d(f'({len(key)} chars)')}")
    print()

    models, dt, code, ok_url, errs = fetch_models(base, key, a.timeout)

    if a.json:
        print(json.dumps(models if models else {"errors":errs}, indent=2, ensure_ascii=False)); return

    if models is None:
        print(f"  {r('✘')} {r('unreachable')}  {d(f'{dt*1000:.0f}ms')}")
        for u,c,e in errs:
            print(f"    {d('→')} {d(u)}  {r(str(c))}  {d((e or '')[:80])}")
        print(); print(d("  tips: check base URL (should end with /v1), check API key, check firewall")); print()
        sys.exit(1)

    print(f"  {g('●')} {b('live')}  {g(f'{len(models)} models')}  {d(f'{dt*1000:.0f}ms')}  {cc(ok_url)}")
    print()

    for mm in models:
        if "owned_by" not in mm: mm["owned_by"]=owner_of(mm)

    cnt=Counter(mm["owned_by"] for mm in models)
    print(b(f"  Providers  {len(cnt)}") + d(f"  │  {len(models)} models"))
    print(d("  "+"─"*54))
    for prov,n in cnt.most_common():
        bar="█"*min(n,22) + "░"*(22-min(n,22))
        sample=next((x for x in models if x["owned_by"]==prov), {})
        print(f"    {cc(prov.ljust(14))} {b(str(n).rjust(3))}  {d(bar)}  {d(caps(sample))}")
    print()

    rows=[x for x in models if (not a.provider or x["owned_by"]==a.provider) and (not a.search or a.search.lower() in x["id"].lower())]
    if a.sort=="provider": rows.sort(key=lambda x:(x["owned_by"], x["id"]))
    elif a.sort=="ctx": rows.sort(key=lambda x: ctx_of(x), reverse=True)
    else: rows.sort(key=lambda x: x["id"])
    total=len(rows)
    shown=rows[:a.limit] if a.limit else rows
    if a.limit and total>a.limit:
        print(b(f"  Models  {len(shown)}/{total}") + d(f"  --limit {a.limit}"))
    else:
        txt=b(f"  Models  {total}")
        if a.provider: txt+=d(f"  provider={a.provider}")
        if a.search: txt+=d(f"  search={a.search}")
        print(txt)
    print(d("  "+"─"*54))
    hdr=f"    {'MODEL':<38} {'OWNER':<12} {'CTX':>6} {'OUT':>6}  CAPS"
    print(d(hdr)); print(d("    "+"─"*38+" "+"─"*12+" "+"─"*6+" "+"─"*6+" "+"─"*6))
    cols={"openai":"g","anthropic":"m","google":"b","gemini":"c","groq":"y","together":"m","openrouter":"b","deepseek":"c","mistral":"y","cohere":"m","qwen":"c","meta":"b"}
    for mm in shown:
        mid=mm["id"]; prov=mm["owned_by"]
        cx=human(ctx_of(mm)); ox=human(out_of(mm)); cp=caps(mm)
        pc=cols.get(prov.lower(),"d")
        prov_s=col(prov.ljust(12), pc, use)
        disp=mid if len(mid)<=38 else mid[:37]+"…"
        dot=g("●") if "R" in cp else d("○")
        print(f"    {dot} {disp:<38} {prov_s} {cx:>6} {ox:>6}  {d(cp)}")
    if a.limit and total>a.limit: print(d(f"    … +{total-a.limit} more (use --limit 0 for all)"))
    print(); print(d("    CAPS: V=vision P=pdf A=audio Vd=video T=tools R=reasoning S=search  ●=reasoning")); print()

    if a.test or a.test_all:
        if not key:
            print(y("  --test requires an API key")); print(); return
        from collections import defaultdict
        by=defaultdict(list)
        for mm in models: by[mm["owned_by"]].append(mm["id"])
        targets=[]
        if a.test_all:
            for prov, ids in by.items():
                if a.provider and prov!=a.provider: continue
                for mid in ids:
                    if a.search and a.search.lower() not in mid.lower(): continue
                    targets.append((prov, mid))
        else:
            for prov, ids in by.items():
                if a.provider and prov!=a.provider: continue
                pick=next((x for x in ids if not a.search or a.search.lower() in x.lower()), None)
                if pick: targets.append((prov, pick))
        print(b(f"  Live Test  {len(targets)} {'models' if a.test_all else 'providers'}") + d(f"  max_tokens=5  timeout={a.timeout}s"))
        print(d("  "+"─"*54))
        def chk(t):
            prov,mid=t
            st,dt,c,msg,_=probe_model(base,key,mid,a.timeout+5)
            return prov,mid,st,int(dt*1000),c,msg
        results=[]
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs={ex.submit(chk,t):t for t in targets}
            for f in concurrent.futures.as_completed(futs):
                prov,mid,st,ms,c,msg=f.result()
                results.append((prov,mid,st,ms,c,msg))
                icon=g("✔") if st=="ok" else r("✘") if st=="err" else y("●") if st=="auth" else d("○")
                stat=g("ok") if st=="ok" else (y(f"auth {c}") if st=="auth" else r(f"{c} {msg[:44]}"))
                print(f"    {icon} {prov.ljust(12)} {mid[:36].ljust(36)} {d(f'{ms}ms'.rjust(7))}  {stat}")
        print()
        ok=[x for x in results if x[2]=="ok"]
        bad=[x for x in results if x[2]!="ok"]
        auth=[x for x in bad if x[2]=="auth"]
        err=[x for x in bad if x[2]=="err"]
        if a.test_all:
            print(b("  ── Summary ──") + d(f"  {len(results)} total"))
            print(d("  "+"─"*54))
            summary_line=f"  {g(f'✔ {len(ok)} working')}  {r(f'✘ {len(bad)} failed') if bad else ''}  {y(f'● {len(auth)} auth') if auth else ''}  {d(f'{sum(x[3] for x in results)//max(1,len(results))}ms avg')}"
            print(summary_line.strip())
            if ok:
                print(); print(f"  {g('✔ Working')} {d(f'({len(ok)})')}")
                for prov,mid,st,ms,c,msg in sorted(ok, key=lambda x: x[1]):
                    print(f"    {g('•')} {mid.ljust(36)} {d(f'{ms}ms')}")
            if bad:
                print(); print(f"  {r('✘ Failed')} {d(f'({len(bad)})')}")
                for prov,mid,st,ms,c,msg in sorted(bad, key=lambda x: (x[2], x[1])):
                    tag=y("auth") if st=="auth" else r(f"{c}")
                    print(f"    {r('•')} {mid.ljust(36)} {d(f'{ms}ms')}  {tag} {d(msg[:46])}")
            print()
        else:
            print(d("  ─") + f"  {g(f'{len(ok)}/{len(results)} ok')}  {r(f'{len(bad)} failed') if bad else ''}  {d(f'{sum(x[3] for x in results)//max(1,len(results))}ms avg')}")
            print()
        def _do_save(out_path=None):
            from pathlib import Path as _P
            probe_dir=_P(__file__).parent
            host_s=host.replace(":","_").replace("/","_")
            default=probe_dir/f"probe-result-{host_s}-{time.strftime('%Y%m%d-%H%M%S')}.json"
            out=_P(out_path) if out_path and out_path!="auto" else default
            if out_path and out_path!="auto":
                out=_P(out_path)
                if not out.is_absolute():
                    out=probe_dir/out if "/" not in str(out) else _P.cwd()/out
            payload={
                "provider": host,
                "base_url": base,
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "summary": {"total": len(results), "working": len(ok), "failed": len(bad), "auth_failed": len(auth)},
                "working": sorted([x[1] for x in ok]),
                "failed": [{"id": x[1], "status": x[2], "code": x[4], "error": x[5], "latency_ms": x[3]} for x in sorted(bad, key=lambda x: x[1])],
            }
            payload["results"]=[{"id": x[1], "provider": x[0], "status": x[2], "code": x[4], "latency_ms": x[3], "error": x[5] if x[2]!="ok" else ""} for x in sorted(results, key=lambda x: x[1])]
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"  {g('✔')} {b('saved')}  {cc(str(out))}  {d(f'{out.stat().st_size} bytes')}")
                print()
                return True
            except Exception as e:
                print(f"  {r('✘')} save failed: {e}"); print()
                return False
        if a.save is not None and results and not a.json:
            _do_save(a.save)
        elif a.test_all and results and not a.json and not a.save:
            ans=""
            if sys.stdin.isatty():
                try: ans=input(col("  ➜ Save test results to JSON? [y/N]: ","c",use)).strip().lower()
                except: ans=""
            elif sys.stdout.isatty():
                try:
                    with open("/dev/tty","r") as tf, open("/dev/tty","w") as to:
                        to.write(col("  ➜ Save test results to JSON? [y/N]: ","c",use)); to.flush()
                        ans=tf.readline().strip().lower()
                except: ans=""
            if ans in ("y","yes","1"):
                p=""
                if sys.stdin.isatty():
                    try: p=input(col("  ➜ File name [enter = auto]: ","c",use)).strip().strip("'\"")
                    except: p=""
                else:
                    try:
                        with open("/dev/tty","r") as tf, open("/dev/tty","w") as to:
                            to.write(col("  ➜ File name [enter = auto]: ","c",use)); to.flush()
                            p=tf.readline().strip().strip("'\"")
                    except: p=""
                _do_save(p if p else "auto")

if __name__=="__main__": main()
