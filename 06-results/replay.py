#!/usr/bin/env python3
"""Verify every submitted file and independently recompute the six objective reports."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def extract(z, prefix, dest):
    seen = set()
    for item in z.infolist():
        if item.is_dir() or not item.filename.startswith(prefix):
            continue
        relative = item.filename[len(prefix):]
        parts = relative.split("/")
        if ("\\" in relative or PurePosixPath(relative).is_absolute()
                or any(part in {"", ".", ".."} or ":" in part for part in parts) or relative in seen):
            raise ValueError("Unexpected archive name")
        seen.add(relative)
        target = dest.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(z.read(item))


def main():
    release = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=release/"02-dataset/development_inputs_v1.0.zip")
    parser.add_argument("--answers", type=Path, default=release/"05-scoring/development_reference_v1.1.zip")
    parser.add_argument("--evidence", type=Path, default=release/"06-results/pilot_evidence.zip")
    args = parser.parse_args()
    scorer = release/"05-scoring/score.py"
    tasks = ["T001", "T002", "T005", "T049", "T050", "T053"]
    checked = 0
    with TemporaryDirectory() as temp, ZipFile(args.evidence) as evidence:
        work = Path(temp)
        with ZipFile(args.input) as inputs:
            extract(inputs, "受测包/", work/"inputs")
        with ZipFile(args.answers) as answers:
            extract(answers, "答案包/", work/"reference")
        records = json.loads(evidence.read("records.json"))
        if set(tasks) != set(records):
            raise ValueError("Unexpected set of submissions")
        for task in tasks:
            expected = {"workpaper.md", "response.json"}
            if task in {"T049", "T050", "T053"}:
                expected.add("corrected.xlsx")
            record = records[task]
            actual_paths = {n for n in evidence.namelist() if n.startswith("submissions/" + task + "/")}
            if ({f"submissions/{task}/{n}" for n in expected} != actual_paths or
                    set(record["output_sha256"]) != expected):
                raise ValueError(f"Submission inventory mismatch: {task}")
            target = work/"answers"/task
            target.mkdir(parents=True)
            for name in sorted(expected):
                data = evidence.read(f"submissions/{task}/{name}")
                if sha(data) != record["output_sha256"][name]:
                    raise ValueError(f"File digest mismatch: {task}/{name}")
                (target/name).write_bytes(data)
                checked += 1
            cmd = [sys.executable, "-I", str(scorer), "--test-root", str(work/"inputs"),
                   "--gold-root", str(work/"reference"), "--results", str(work/"answers"),
                   "--task", task]
            current = json.loads(subprocess.check_output(cmd))
            stored = json.loads(evidence.read(f"objective/{task}.json"))
            if {key: current[key] for key in stored} != stored:
                raise ValueError(f"Objective report mismatch: {task}")
            print(f"{task} {current['auto_score']:.4f}/80")
    print(f"Verified {len(tasks)} objective reports and {checked} original submissions files.")


if __name__ == "__main__":
    main()
