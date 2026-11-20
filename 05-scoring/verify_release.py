"""Verify the two public archives and regression-test scoring. Python 3.10+."""
import csv
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

sys.dont_write_bytecode = True
from score import score
from verify_monthly import verify_monthly

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "02-dataset"
SCORING = ROOT / "05-scoring"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def json_out(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def checked(condition, message):
    if not condition:
        raise ValueError(message)


def extract_public(archive, destination, allowed_roots):
    paths = []
    with ZipFile(archive) as z:
        checked(z.testzip() is None, f"ZIP CRC failed: {archive.name}")
        for member in z.infolist():
            name = PurePosixPath(member.filename)
            checked(not name.is_absolute() and ".." not in name.parts and "\\" not in member.filename,
                    f"Invalid member path: {name}")
            checked(any(parent in name.parents for parent in allowed_roots), f"Out-of-split entry: {name}")
            checked((member.external_attr >> 16) & 0o170000 != 0o120000, f"Symlink entry: {name}")
            checked(name not in paths and not member.is_dir(), f"Duplicate or empty folder: {name}")
            paths.append(name)
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(member))
    return len(paths)


def verify_r1(temp):
    """Check the revised single-task pairing and exercise its independent scoring rules."""
    root = temp/"t005-r1"
    source = PurePosixPath("受测包/案例01/T005")
    inputs = DATA/"T005-r1_单题受测包.zip"
    answers = SCORING/"T005-r1_单题评分包_v1.2.zip"
    checked(extract_public(inputs, root, {source}) == 6, "T005-r1 unexpected input inventory")
    with ZipFile(answers) as z:
        allowed = {"答案包/专家答案/T005/"+n for n in ("input_manifest.json","gold.json","reference_response.json",
                    "manual_review_template.json","reference_workpaper.md","专家解析.md")}
        allowed.update({"答案包/04_人工评分量规.md","答案包/00_运行说明.md"})
        checked(set(z.namelist()) == allowed and len(z.namelist()) == len(allowed), "T005-r1 unexpected answer inventory")
        for name in allowed:
            safe = root/name
            safe.parent.mkdir(parents=True, exist_ok=True)
            safe.write_bytes(z.read(name))
    gold = root/"答案包/专家答案/T005"
    question = root.joinpath(*source.parts)
    manifest = read_json(question/"input_manifest.json")
    checked(manifest == read_json(gold/"input_manifest.json") and len(manifest["sha256"]) == 5,
            "T005-r1 inconsistent paired input manifests")
    for name, digest in manifest["sha256"].items():
        checked(hashlib.sha256((question/name).read_bytes()).hexdigest() == digest, "T005-r1 input changed")
    result = root/"results/T005"
    result.mkdir(parents=True)
    workpaper = (gold/"reference_workpaper.md").read_text(encoding="utf-8")
    cases = [
        ("reference", [], None, "REVIEW", workpaper, 80, []),
        ("candidate list", ["L003","L099"], None, "REVIEW", workpaper, 76.6667, []),
        ("unknown difference populated", [], "0.00", "REVIEW", workpaper, 76.6667, []),
        ("unsupported match", ["L003"], "0.00", "MATCHED", workpaper, 75.5556, ["UNSUPPORTED_DEFINITE_CONCLUSION"]),
        ("reason requires human review", [], None, "REVIEW", "待核", 80, []),
    ]
    for name, ids, difference, status, note, expected, fatal in cases:
        r = read_json(gold/"reference_response.json")
        target = next(row for row in r["items"] if row["item_id"] == "B003")
        target.update(status=status, value={"ledger_ids": ids, "difference": difference})
        json_out(result/"response.json", r)
        (result/"workpaper.md").write_text(note, encoding="utf-8")
        report = score(root/"受测包", root/"答案包", root/"results", "T005")
        checked(report["auto_score"] == expected and report["fatal_errors"] == fatal
                and report["final_score"] is None and report["manual_score"] is None, f"T005-r1 {name}")
    print(f"Verified T005-r1 input pairing and {len(cases)} single-task scoring controls.")


def main():
    with (DATA / "开发集划分.csv").open(encoding="utf-8-sig") as stream:
        split = list(csv.DictReader(stream))
    with (DATA / "首轮12题试评清单.csv").open(encoding="utf-8-sig") as stream:
        pilot = list(csv.DictReader(stream))
    ids = {r["题号"] for r in split}
    checked(len(split) == len(ids) == 24 and {r["案例"] for r in split} == {"F01","F03","F07","F09"},
            "Public split differs from the four development families")
    public_ids = {f"T{n:03d}" for start in (1, 13, 37, 49) for n in range(start, start + 6)}
    checked(ids == public_ids, "Public task IDs differ from release")
    pilot_ids = {r["题目"] for r in pilot}
    checked(len(pilot) == len(pilot_ids) == 12 and pilot_ids == {r["题号"] for r in split if r["首轮试评"]=="是"},
            "Pilot membership differs from the split listing")
    allowed_inputs = {PurePosixPath(row["输入目录"]) for row in split}
    allowed_gold = {PurePosixPath("答案包/专家答案") / tid for tid in ids}
    with tempfile.TemporaryDirectory() as temp:
        stage = Path(temp)
        counts = [extract_public(DATA/"development_inputs_v1.0.zip", stage, allowed_inputs),
                  extract_public(SCORING/"development_reference_v1.1.zip", stage, allowed_gold)]
        test, gold, results = stage/"受测包", stage/"答案包", stage/"reference-fixtures"
        def run(tid):
            return score(test, gold, results, tid)
        for row in split:
            tid = row["题号"]
            source = stage/row["输入目录"]
            ref = gold/"专家答案"/tid
            manifest = read_json(source/"input_manifest.json")
            checked(manifest == read_json(ref/"input_manifest.json"), f"{tid}: input and gold manifests differ")
            target_paths = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()}
            checked(target_paths == set(manifest["sha256"]) | {"input_manifest.json"}, f"{tid}: unlisted inputs")
            for name, digest in manifest["sha256"].items():
                checked(hashlib.sha256((source/name).read_bytes()).hexdigest() == digest,
                        f"{tid}: SHA-256 mismatch at {name}")
            task_gold = read_json(ref/"gold.json")
            expected = {"input_manifest.json","gold.json","reference_response.json","manual_review_template.json","专家解析.md"}
            if task_gold.get("workbook"):
                expected.add("corrected.xlsx")
            checked({p.name for p in ref.iterdir()} == expected, f"{tid}: unexpected answer files")
            dest = results/tid
            dest.mkdir(parents=True)
            shutil.copyfile(ref/"reference_response.json", dest/"response.json")
            shutil.copyfile(ref/"专家解析.md", dest/"workpaper.md")
            if task_gold.get("workbook"):
                shutil.copyfile(ref/"corrected.xlsx", dest/"corrected.xlsx")
            report = run(tid)
            checked(report["auto_score"] == 80 and report["final_score"] is None
                    and not report["fatal_errors"] and not report["critical_errors"]
                    and not report["gate_errors"], f"{tid}: reference fixture failed")
        # Manipulate temporary public answer fixtures only. These are scoring controls.
        response = results/"T005"/"response.json"
        data = read_json(response)
        ambiguous = next(v for v in data["items"] if v["item_id"]=="B003")
        ambiguous.update(status="MATCHED", value={"ledger_ids":["L003"], "difference":"0.00"})
        json_out(response, data)
        report = run("T005")
        checked("UNSUPPORTED_DEFINITE_CONCLUSION" in report["fatal_errors"] and
                "B003" in report["critical_errors"], "Forced-match control failed")
        response = results/"T049"/"response.json"
        data = read_json(response)
        data["scope"]["entity"] = data["scope"]["entity"].removesuffix("（虚构）")
        ratio = next(v for v in data["items"] if v["item_id"]=="月结!B6")
        ratio["evidence_refs"] = ["原始材料/close.xlsx#月结!"+c for c in ("B6","B2","B5")]
        json_out(response, data)
        report = run("T049")
        checked(report["auto_score"] == 77.5 and report["components"]["evidence_locations"] == 10
                and "SCOPE_INCORRECT" in report["gate_errors"], "Scope and evidence-chain control failed")
        ratio["evidence_refs"].remove("原始材料/close.xlsx#月结!B5")
        json_out(response, data)
        checked(run("T049")["components"]["evidence_locations"] < 10, "Broken evidence chain got full credit")
        verify_monthly(ROOT, stage, gold/"专家答案/T053", checked)
        verify_r1(stage)
    print(f"Verified {len(split)} public inputs and reference submissions; {len(pilot)} pilot tasks.")
    print(f"Verified {counts[0]} input files and {counts[1]} reference files, by item allowlist.")
    print("All 24 reference submissions received 80/80 automated points. Three scoring controls passed.")
    print("Reference checks do not grade the Accounting plugin; human scores remain unset.")


if __name__ == "__main__":
    main()
