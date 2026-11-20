#!/usr/bin/env python3
"""Replay the twelve published first-round submissions with the public v1.1 scorer."""
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


def main():
    release = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=release/"02-dataset/development_inputs_v1.0.zip")
    parser.add_argument("--answers", type=Path, default=release/"05-scoring/development_reference_v1.1.zip")
    parser.add_argument("--evidence", type=Path, default=release/"06-results/pilot_evidence.zip")
    parser.add_argument("--supplement", type=Path, default=release/"06-results/round1_supplement.zip")
    parser.add_argument("--summary", type=Path, default=release/"06-results/scores.csv")
    args = parser.parse_args()
    scorer = release/"05-scoring/score.py"
    scorer_digest = sha(scorer.read_bytes())
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
                require(record["task_id"] == task and record["public_scorer_sha256"] == scorer_digest,
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
            cmd = [sys.executable, "-I", str(scorer), "--test-root", str(work/"inputs"),
                   "--gold-root", str(work/"reference"), "--results", str(work/"submissions"),
                   "--task", task]
            current = json.loads(subprocess.check_output(cmd))
            full = read_json(supplement, f"scores/{task}.json")
            require(current == full, f"Full score report mismatch: {task}")
            if task in FIRST:
                old = read_json(pilot, f"objective/{task}.json")
                require(old == {k: current[k] for k in old}, f"Historical score mismatch: {task}")
            if archived_review:
                require(current["final_score"] == archived_review["final_score"]
                        and current["status"] == archived_review["status"], "T002 manual record mismatch")
            row = summary[task]
            require(int(row["答卷文件数"]) == n, f"Score summary file count mismatch: {task}")
            for key, col in (("auto_score", "客观分/80"), ("manual_score", "人工分/20"),
                             ("final_score", "最终分/100")):
                value = float(row[col]) if row[col] else None
                require(current[key] == value, f"Score summary mismatch: {task}/{key}")
            print(f"{task} {current['auto_score']:.4f}/80")
        compared = verify_comparisons(supplement, work)
    require(count == 27, "Original submission file count mismatch")
    print(f"Verified {len(tasks)} full score reports, {count} original submission files, "
          f"{compared} supplemental item comparisons and the first-round score table.")


if __name__ == "__main__":
    main()
