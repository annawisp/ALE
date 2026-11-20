#!/usr/bin/env python3
"""Offline preparation, one isolated Codex CLI+Accounting process per task, host-only grading."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import uuid
from zipfile import ZipFile

from broker import relay

PLUGIN_DIGEST = "db7625cc1ed9daca7a573c522d0bb276f5efcf27a51acd63c5a8edfc91d067fb"
PUBLIC_SCORER_DIGEST = "4c3e1b21e1a0505bb13d93aa69df62ee6d22e2b1c2caf2f84393252c0f71eedb"
FULL_SCORER_DIGEST = "61d3e842c55f506439e08e7a77a39bfa970114729d783e1952c3dd9d2c51ab84"
APPROVED_ANSWER_PACKAGES = {
    "8ad5eb18097ed4d18beea61a748e6ae75b1faf399769ad33bfb1fb1ab6872263": PUBLIC_SCORER_DIGEST,
    "b7f70734a4d8856e7571d74216d80410f0901adbe91cbee985193d73c6d70c32": PUBLIC_SCORER_DIGEST,
    "9e20c9035d698d93ed48c6a3de8ba0f2bbe16fc67a20348b4222df5f29e1d8cc": FULL_SCORER_DIGEST,
}
PUBLIC_TASKS = {f"T{n:03d}" for start in (1, 13, 37, 49) for n in range(start, start + 6)}
READY = {"DELIVERED","INVALID_DELIVERY"}
IMAGE = "accounting-codex-pilot:1"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, obj):
    temp=path.with_name(path.name+".tmp")
    temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    temp.replace(path)


def entries(root):
    values={}
    for f in sorted(root.rglob("*")):
        if f.is_symlink() or not (f.is_file() or f.is_dir()):
            raise ValueError("Unexpected link or special file: "+str(f))
        if f.is_file():
            values[f.relative_to(root).as_posix()]=digest(f.read_bytes())
    return values


def tree_hash(files):
    return digest("".join(f"{h}  {n}\n" for n,h in sorted(files.items())).encode())


def unpack(zipfile, prefix, dest):
    names=set()
    for i in zipfile.infolist():
        if i.is_dir() or not i.filename.startswith(prefix):
            continue
        name=i.filename[len(prefix):]
        if (not name or "\\" in name or any(p in {".","..",""} for p in name.split("/"))
            or PurePosixPath(name).is_absolute() or name in names):
            raise ValueError("Unsafe or duplicate archive member")
        names.add(name)
        path=dest/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(zipfile.read(i))


def prepare(a):
    if len(set(a.tasks))!=len(a.tasks) or not all(re.fullmatch(r"T[0-9]{3,}",t) for t in a.tasks):
        raise ValueError("Expected unique task IDs such as T005")
    if a.run_dir.exists():
        raise ValueError("Run directory exists. Use run to resume, or choose a new directory.")
    a.run_dir.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=a.run_dir.parent) as tmp:
        stage=Path(tmp)/"batch"
        stage.mkdir()
        plan={"created":stamp(),"source_sha256":digest(a.input.read_bytes()),"tasks":{}}
        with ZipFile(a.input) as z:
            for t in a.tasks:
                found=[n for n in z.namelist() if re.fullmatch(r"受测包/案例[0-9]+/"+t+r"/input_manifest\.json",n)]
                if len(found)!=1:
                    raise ValueError("Case missing/ambiguous in archive: "+t)
                prefix=found[0].rsplit("/",1)[0]+"/"
                family=prefix.split("/")[1]
                dest=stage/"inputs"/family/t
                unpack(z,prefix,dest)
                info=read_json(dest/"input_manifest.json")
                actual=entries(dest)
                if (info["task_id"]!=t or {n:h for n,h in actual.items() if n!="input_manifest.json"}!=info["sha256"]
                    or read_json(dest/"回答模板.json")["task_id"]!=t):
                    raise ValueError("Task manifest or template mismatch: "+t)
                files=["response.json","workpaper.md"]
                if "corrected.xlsx" in (dest/"任务书.md").read_text(encoding="utf-8-sig"):
                    files.append("corrected.xlsx")
                plan["tasks"][t]={"family":family,"inputs":actual,"required":files}
        write(stage/"plan.json",plan)
        stage.rename(a.run_dir)
    print(f"Prepared {len(a.tasks)} cases in {a.run_dir}")


def genuine_skill_read(events, path):
    for ev in events:
        it=ev.get("item",{})
        if ev.get("type")!="item.completed" or it.get("type")!="command_execution" or it.get("exit_code")!=0:
            continue
        text=it.get("aggregated_output","")
        try:
            args=shlex.split(it.get("command",""))
            if len(args)==3 and Path(args[0]).name in {"bash","sh","dash"} and args[1] in {"-c","-lc"}:
                args=shlex.split(args[2])
        except ValueError:
            continue
        if (len(args)==2 and Path(args[0]).name=="cat" and args[1]==path
            and "name: accounting" in text and "# Accounting" in text):
            return True
    return False


def run_one(t, case, a, key, image_id):
    run=a.run_dir
    src=run/"inputs"/case["family"]/t
    if entries(src)!=case["inputs"]:
        raise ValueError("Input bytes changed: "+t)
    attempt=run/"attempts"/t/uuid.uuid4().hex
    out=attempt/"out"
    out.mkdir(parents=True)
    name="acct-exam-"+attempt.name
    rec={"task_id":t,"attempt":attempt.name,"started":stamp(),"model":a.model,
         "plugin_sha256":PLUGIN_DIGEST,"runtime":"codex_cli_docker_native_local_install",
         "docker_image_id":image_id,"status":"INTERRUPTED","scorable":False}
    write(attempt/"receipt.json",rec)
    with tempfile.TemporaryDirectory(prefix="acct-sock-") as tmp:
        args=["docker","run","--name",name,"--rm","--network","none","--read-only",
              "--cap-drop","ALL","--security-opt","no-new-privileges","--pids-limit","256",
              "--user",f"{os.getuid()}:{os.getgid()}",
              "--tmpfs","/tmp:rw,nosuid,nodev,mode=1777,size=512m",
              "--tmpfs",f"/run/exam-home:rw,nosuid,nodev,mode=0700,uid={os.getuid()},gid={os.getgid()},size=128m"]
        for path,dst,ro in [(src,"/exam/input",True),(out,"/exam/out",False),(a.plugin_dir,"/catalog/plugins/accounting",True),(Path(tmp),"/relay",True)]:
            if "," in str(path):raise ValueError("Docker mount paths cannot contain commas")
            args+=["--mount",f"type=bind,src={path.resolve()},dst={dst}"+(",readonly" if ro else "")]
        args += [image_id,t,a.model]
        try:
            with relay(tmp,key,a.model), (attempt/"events.jsonl").open("w") as log, (attempt/"stderr.txt").open("w") as err:
                pr=subprocess.run(args,stdout=log,stderr=err,timeout=a.timeout,check=False)
            rec["exit_code"]=pr.returncode
            ev=[]
            with (attempt/"events.jsonl").open(encoding="utf-8",errors="replace") as log:
                for line in log:
                    try: row=json.loads(line)
                    except ValueError:continue
                    if isinstance(row,dict):ev.append(row)
            plugin=next((e for e in ev if e.get("type")=="pilot.plugin_ready"),{})
            thread=next((e.get("thread_id") for e in ev if e.get("type")=="thread.started"),None)
            loaded=bool(plugin) and genuine_skill_read(ev,plugin["skill_path"])
            rec.update(plugin=plugin,thread_id=thread,loaded=loaded)
            if pr.returncode or not any(e.get("type")=="turn.completed" for e in ev):
                rec["status"]="RUNNER_ERROR"
            elif not loaded or not thread:
                rec["status"]="ACTIVATION_UNVERIFIED"
            elif entries(src)!=case["inputs"]:
                rec["status"]="INPUT_INTEGRITY_ERROR"
            else:
                hashes,issues={},[]
                for n in case["required"]:
                    f=out/n
                    if not f.is_file() or f.is_symlink() or f.stat().st_size==0:
                        issues.append("Missing, linked or empty: "+n)
                    else:hashes[n]=digest(f.read_bytes())
                if "response.json" in hashes:
                    try:
                        answer=read_json(out/"response.json")
                        if not isinstance(answer,dict) or answer.get("task_id")!=t:issues.append("Incorrect task_id")
                    except (UnicodeError,ValueError):issues.append("Invalid response.json")
                rec.update(status="INVALID_DELIVERY" if issues else "DELIVERED",scorable=True,output_sha256=hashes,delivery_issues=issues)
        except subprocess.TimeoutExpired:
            rec["status"]="TIMEOUT"
        finally:
            subprocess.run(["docker","rm","-f",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
            rec["finished"]=stamp()
            write(attempt/"receipt.json",rec)
    print(t,rec["status"],flush=True)
    return rec


def receipts(run,t):
    paths=(run/"attempts"/t).glob("*/receipt.json")
    return sorted(((p,read_json(p)) for p in paths),key=lambda v:v[1]["started"])


def run_batch(a):
    plan=read_json(a.run_dir/"plan.json")
    if tree_hash(entries(a.plugin_dir))!=PLUGIN_DIGEST:
        raise ValueError("The selected Accounting 1.0.1 official package does not match the pinned directory.")
    if not shutil.which("docker"):raise ValueError("Docker is unavailable")
    image_id=subprocess.check_output(["docker","image","inspect","--format","{{.Id}}",a.image],text=True).strip()
    key=os.environ.get("OPENAI_API_KEY")
    if not key:raise ValueError("Set OPENAI_API_KEY in the host process; never place it in the kit or input.")
    config_file=a.run_dir/"configuration.json"
    configuration={"model":a.model,"image_id":image_id,"plugin_sha256":PLUGIN_DIGEST}
    if config_file.exists() and read_json(config_file)!=configuration:
        raise ValueError("Model, image or plugin differs from the original batch. Use a new run directory.")
    if not config_file.exists():
        write(config_file,configuration)
    config={"model":a.model,"docker_image_id":image_id,"plugin_digest":PLUGIN_DIGEST,
            "runner_digest":digest(Path(__file__).read_bytes()),
            "broker_digest":digest(Path(__file__).with_name("broker.py").read_bytes())}
    frozen=a.run_dir/"experiment.json"
    if frozen.exists() and config!=read_json(frozen):
        raise ValueError("Model, runner or image changed. Prepare a separate run directory.")
    write(frozen,config)
    failed=False
    for t,case in plan["tasks"].items():
        previous=receipts(a.run_dir,t)
        if any(r["status"] in READY for _,r in previous):
            print(t,"already submitted");continue
        if previous and not a.retry:
            print(t,"prior infrastructure attempt; use --retry");failed=True;continue
        result=run_one(t,case,a,key,image_id)
        failed |= result["status"] not in READY
    return 2 if failed else 0


def score_batch(a):
    plan=read_json(a.run_dir/"plan.json")
    reports=a.run_dir/"scores"
    summaries=[]
    archive=a.answers.read_bytes()
    answers_sha=digest(archive)
    if answers_sha not in APPROVED_ANSWER_PACKAGES:
        raise ValueError("Scoring configuration: this reference archive does not match an approved v1.2 package")
    with tempfile.TemporaryDirectory(prefix="acct-score-") as tmp:
        private=Path(tmp)
        with ZipFile(io.BytesIO(archive)) as z:
            unpack(z,"答案包/",private/"gold")
        embedded=private/"gold"/"score.py"
        public=Path(__file__).resolve().parents[1]/"05-scoring"/"score.py"
        scorer=a.scorer or (embedded if embedded.is_file() else public)
        if not scorer.is_file():
            raise ValueError("Scoring configuration: cannot locate the v1.2 scoring program")
        scorer_sha=digest(scorer.read_bytes())
        if scorer_sha!=APPROVED_ANSWER_PACKAGES[answers_sha]:
            raise ValueError("Scoring configuration: the selected scorer does not match this v1.2 reference package")
        if scorer_sha==PUBLIC_SCORER_DIGEST and set(plan["tasks"])-PUBLIC_TASKS:
            raise ValueError("Scoring configuration: tasks outside the public split require the full private v1.2 package")
        for t,case in plan["tasks"].items():
            gold=private/"gold"/"专家答案"/t
            manifest=gold/"input_manifest.json"
            if (not (gold/"gold.json").is_file() or not manifest.is_file()
                    or digest(manifest.read_bytes())!=case["inputs"]["input_manifest.json"]):
                raise ValueError("Scoring configuration: reference task or input manifest mismatch for "+t)
        grading={"baseline":"v1.2","scorer_sha256":scorer_sha,"answers_sha256":answers_sha}
        grading_file=reports/"grading.json"
        if grading_file.exists():
            if read_json(grading_file)!=grading:
                raise ValueError("Scoring configuration: this batch already uses a different scorer or reference archive")
        elif reports.is_dir() and any(reports.iterdir()):
            raise ValueError("Scoring configuration: the existing reports have no recorded grading version")
        else:
            reports.mkdir(exist_ok=True)
            write(grading_file,grading)
        for t,case in plan["tasks"].items():
            approved=[(p,r) for p,r in receipts(a.run_dir,t) if r.get("scorable") and r.get("status") in READY]
            if not approved:
                summaries.append({"task_id":t,"status":"NO_VERIFIED_RUN","auto_score":None});continue
            path,rec=approved[0]
            src=a.run_dir/"inputs"/case["family"]/t
            if entries(src)!=case["inputs"]:raise ValueError("Input bytes changed: "+t)
            safe=private/"answers"/t
            safe.mkdir(parents=True)
            for n,h in rec["output_sha256"].items():
                if n not in case["required"]:raise ValueError("Unexpected output in receipt")
                f=path.parent/"out"/n
                if f.is_symlink() or digest(f.read_bytes())!=h:raise ValueError("Submission changed: "+t)
                shutil.copyfile(f,safe/n)
            proc=subprocess.run([sys.executable,"-I",str(scorer.resolve()),"--test-root",str(a.run_dir/"inputs"),
                            "--gold-root",str(private/"gold"),"--results",str(private/"answers"),"--task",t],
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                if proc.returncode:raise ValueError("Scorer exited with error")
                data=json.loads(proc.stdout)
            except ValueError:
                data={"task_id":t,"status":"SCORER_ERROR","auto_score":None}
                (reports/(t+".stderr")).write_text(proc.stderr)
            write(reports/(t+".json"),data)
            summaries.append({"task_id":t,"attempt":rec["attempt"],"status":data.get("status"),
                              "auto_score":data.get("auto_score"),"manual_score":None})
    write(reports/"summary.json",summaries)
    for row in summaries:print(row["task_id"],row["status"],row["auto_score"])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="phase",required=True)
    prep=sub.add_parser("prepare");prep.add_argument("--input",type=Path,required=True)
    prep.add_argument("--tasks",nargs="+",required=True);prep.set_defaults(fn=prepare)
    run=sub.add_parser("run");run.add_argument("--plugin-dir",type=Path,required=True);run.add_argument("--model",required=True)
    run.add_argument("--image",default=IMAGE);run.add_argument("--timeout",type=int,default=900)
    run.add_argument("--retry",action="store_true",help="Repeat infrastructure failures in new attempts, never overwrite prior records")
    run.set_defaults(fn=run_batch)
    grade=sub.add_parser("score");grade.add_argument("--answers",type=Path,required=True)
    grade.add_argument("--scorer",type=Path,help="Use this pinned v1.2 scorer instead of the bundled or public scorer")
    grade.set_defaults(fn=score_batch)
    for command in (prep,run,grade):command.add_argument("--run-dir",type=Path,required=True)
    a=parser.parse_args()
    a.run_dir=a.run_dir.resolve()
    cm=contextlib.nullcontext() if a.phase=="prepare" else (a.run_dir/".lock").open("a")
    with cm as lock:
        if lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return a.fn(a)


if __name__=="__main__":
    try:sys.exit(main())
    except (OSError,ValueError,KeyError,subprocess.CalledProcessError) as e:
        print("ERROR:",e,file=sys.stderr);sys.exit(2)
