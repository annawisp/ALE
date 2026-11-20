#!/usr/bin/env python3
"""Re-evaluate 12 original submissions and reconcile the results to the published score table."""
import argparse
import csv
import hashlib
from io import StringIO
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

FIRST = ("T001", "T002", "T005", "T049", "T050", "T053")
ADDED = ("T013", "T014", "T017", "T037", "T038", "T041")
WORKBOOKS = {"T049", "T050", "T053"}
V1_1_SCORER_SHA256 = "418ecc8fcd48fbe51d2698596868654ddb536d2f899249359f46fbe44c965cb4"


def require(test, message):
    if not test:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(archive, name):
    return json.loads(archive.read(name))


def extract(archive, prefix, dest):
    seen = set()
    for item in archive.infolist():
        if item.is_dir() or not item.filename.startswith(prefix):
            continue
        relative = item.filename[len(prefix):]
        parts = relative.split("/")
        require("\\" not in relative and not PurePosixPath(relative).is_absolute()
                and all(part not in {"", ".", ".."} and ":" not in part for part in parts)
                and relative not in seen, "Unexpected archive name")
        seen.add(relative)
        target = dest.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(item))


def unpack_submission(archive, record, task, folder):
    expected = {"workpaper.md", "response.json"}
    if task in WORKBOOKS:
        expected.add("corrected.xlsx")
    prefix = f"submissions/{task}/"
    names = sorted(name[len(prefix):] for name in archive.namelist() if name.startswith(prefix))
    require(names == sorted(expected) and set(record["output_sha256"]) == expected,
            f"Submission inventory mismatch: {task}")
    folder.mkdir(parents=True)
    for name in names:
        data = archive.read(prefix + name)
        require(sha(data) == record["output_sha256"][name], f"File digest mismatch: {task}/{name}")
        (folder/name).write_bytes(data)
    return len(names)


def historical_manual_input(archive, target):
    # These anonymous placeholders activate the scorer's historical-score branch.
    # The scores come exclusively from the redacted historical record.
    review = read_json(archive, "historical/T002_human.json")
    criteria = review["criterion_scores"]
    require(set(criteria) == {f"{prefix}{n}" for prefix in ("P", "E") for n in range(1, 6)},
            "T002 archived criteria incomplete")
    fields = {"professional_reasoning": "P", "evidence_semantics": "E"}
    for key, prefix in fields.items():
        require(sum(criteria[f"{prefix}{n}"] for n in range(1, 6)) == review[key],
                f"T002 archived criterion total mismatch: {key}")
    data = {key: review[key] for key in fields}
    data.update(reviewer="anonymous historical replay", reviewed_at="historical record", fatal_errors=[])
    (target/"manual_review.json").write_text(json.dumps(data), encoding="utf-8")
    return review


def verify_comparisons(source, work):
    rows = list(csv.DictReader(StringIO(source.read("item_comparison.csv").decode("utf-8-sig"), newline="")))
    actual = {}
    reference = {}
    for task in ADDED:
        for table, path in (
                (actual, work/"submissions"/task/"response.json"),
                (reference, work/"reference"/"专家答案"/task/"gold.json")):
            for item in json.loads(path.read_text(encoding="utf-8-sig"))["items"]:
                key = (task, item["item_id"])
                require(key not in table, f"Duplicate item: {key}")
                table[key] = item
    keys = [(row["题号"], row["题项ID"]) for row in rows]
    require(len(rows) == 28 and len(set(keys)) == 28 and set(keys) == set(actual) == set(reference),
            "28-row comparison inventory mismatch")
    for key, row in zip(keys, rows):
        for prefix, item in (("实际", actual[key]), ("参考", reference[key])):
            require(row[prefix+"状态"] == item["status"]
                    and json.loads(row[prefix+"值"]) == item["value"]
                    and row[prefix+"引用"].splitlines() == item["evidence_refs"],
                    f"Item comparison mismatch: {key}, {prefix}")
    return len(rows)


def replay(scorer, work, task):
    cmd = [sys.executable, "-I", str(scorer), "--test-root", str(work/"inputs"),
           "--gold-root", str(work/"reference"), "--results", str(work/"submissions"),
           "--task", task]
    return json.loads(subprocess.check_output(cmd))


def verify_workbook_checks(pilot):
    checks = read_json(pilot, "independent_checks/T053.json")
    expected_scenarios = {"original", "change_expense", "missing_as_zero",
                          "zero_budget", "supply_missing_amount"}
    cases = checks["scenarios"]
    require(checks["state"] == "complete" and checks["all_checked_values_match"]
            and len(cases) == 5 and {x["scenario"] for x in cases} == expected_scenarios,
            "T053: incomplete independent recalculation")
    for case in cases:
        expected, actual = case["expected"], case["actual"]
        require(not case["differences"] and set(expected) == set(actual) == {"B2","B3","B4","B6"},
                f"T053: incorrect coverage in {case['scenario']}")
        for key, value in expected.items():
            observed = actual[key]
            if isinstance(value, (int, float)) and isinstance(observed, (int, float)):
                matches = abs(value - observed) <= 1e-7
            else:
                matches = value == observed
            require(matches, f"T053 independent recalculation mismatch: {case['scenario']}/{key}")


def verify_current_report(task, archived, current):
    # Version-specific interpreter descriptions can differ without changing scores.
    expected = {k: v for k, v in archived.items() if k != "notes"}
    if task == "T053":
        require(archived["auto_score"] == 65.7143 and archived["components"]["delivery"] == 5
                and archived["components"]["evidence_locations"] == 0.7143
                and archived["fatal_errors"] == ["INVALID_EVIDENCE_REFERENCE"],
                "T053 historical scoring baseline changed")
        expected["auto_score"] = 70.7143
        expected["components"] = dict(archived["components"], delivery=10)
        # The confirmed workbook formulas clear their three old interpretation warnings.
        warnings = {f"需人工核验等价公式：月结!{cell} COUNT" for cell in ("B2", "B4", "B6")}
        require({v for v in archived["details"] if isinstance(v,str)} == warnings,
                "T053: historical workbook warnings do not match the adjudication")
        expected["details"] = [v for v in archived["details"] if not isinstance(v, str)]
    require({k: v for k, v in current.items() if k != "notes"} == expected,
            f"Current score report differs from the approved results: {task}")


def main():
    release = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=release/"02-dataset/development_inputs_v1.0.zip")
    parser.add_argument("--answers", type=Path, default=release/"05-scoring/development_reference_v1.1.zip")
    parser.add_argument("--evidence", type=Path, default=release/"06-results/pilot_evidence.zip")
    parser.add_argument("--supplement", type=Path, default=release/"06-results/round1_supplement.zip")
    parser.add_argument("--summary", type=Path, default=release/"06-results/scores.csv")
    parser.add_argument("--scorer", type=Path, default=release/"05-scoring/score.py")
    parser.add_argument("--historical-scorer", type=Path,
                        help="Also reproduce the archived reports with an exact v1.1 scorer file.")
    args = parser.parse_args()
    if args.historical_scorer:
        require(sha(args.historical_scorer.read_bytes()) == V1_1_SCORER_SHA256,
                "The historical replay requires the exact archived v1.1 scorer")
    with args.summary.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    summary = {r["题号"]: r for r in rows}
    tasks = sorted(FIRST + ADDED)
    require(len(rows) == len(tasks) and set(summary) == set(tasks), "Score summary inventory mismatch")
    count = 0
    with TemporaryDirectory() as temp, ZipFile(args.evidence) as pilot, ZipFile(args.supplement) as supplement:
        work = Path(temp)
        with ZipFile(args.input) as inputs, ZipFile(args.answers) as gold:
            extract(inputs, "受测包/", work/"inputs")
            extract(gold, "答案包/", work/"reference")
        original_records = read_json(pilot, "records.json")
        new_records = read_json(supplement, "records.json")
        require(set(original_records) == set(FIRST) and set(new_records) == set(ADDED),
                "Run receipt task inventory mismatch")
        for task in tasks:
            if task in new_records:
                archive, record = supplement, new_records[task]
                require(record["task_id"] == task and record["public_scorer_sha256"] == V1_1_SCORER_SHA256,
                        f"Scorer or receipt identity mismatch: {task}")
                matches = list((work/"inputs").glob("案例*/"+task))
                require(len(matches) == 1, f"Unexpected input folders: {task}")
                folder = matches[0]
                fingerprints = {p.relative_to(folder).as_posix(): sha(p.read_bytes())
                                for p in folder.rglob("*") if p.is_file()}
                require(fingerprints == record["input_sha256"], f"Input receipt mismatch: {task}")
            else:
                archive, record = pilot, original_records[task]
            target = work/"submissions"/task
            n = unpack_submission(archive, record, task, target)
            count += n
            archived_review = historical_manual_input(pilot, target) if task == "T002" else None
            current = replay(args.scorer, work, task)
            full = read_json(supplement, f"scores/{task}.json")
            if args.historical_scorer:
                require(replay(args.historical_scorer, work, task) == full,
                        f"Archived full score report mismatch: {task}")
            if task in FIRST:
                old = read_json(pilot, f"objective/{task}.json")
                require(old == {k: full[k] for k in old}, f"Historical score mismatch: {task}")
            verify_current_report(task, full, current)
            if archived_review:
                require(current["final_score"] == archived_review["final_score"]
                        and current["status"] == archived_review["status"], "T002 manual record mismatch")
            row = summary[task]
            require(int(row["答卷文件数"]) == n, f"Score summary file count mismatch: {task}")
            delta = round(current["auto_score"] - full["auto_score"], 4)
            require(float(row["历史客观分（v1.1）/80"]) == full["auto_score"] and
                    float(row["同卷重评调整"]) == delta and delta == (5 if task == "T053" else 0),
                    f"Score reconciliation mismatch: {task}")
            for key, col in (("auto_score", "当前客观分/80"), ("manual_score", "人工分/20"),
                             ("final_score", "最终分/100")):
                value = float(row[col]) if row[col] else None
                require(current[key] == value, f"Score summary mismatch: {task}/{key}")
            print(f"{task} {full['auto_score']:.4f} {delta:+.4f} = {current['auto_score']:.4f}/80")
        compared = verify_comparisons(supplement, work)
        verify_workbook_checks(pilot)
    require(count == 27, "Original submission file count mismatch")
    print(f"Verified {len(tasks)} reevaluations, {count} original files, {compared} supplemental "
          "item comparisons, 20 archived T053 workbook checks, and the current score table.")


if __name__ == "__main__":
    main()
