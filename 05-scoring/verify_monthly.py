"""Independent business-value probes and workbook controls for the public monthly-close layout."""
import json
import math
import shutil
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import score as scorer

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RELS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

def sheet_member(archive, title):
    sheets = ET.fromstring(archive.read("xl/workbook.xml"))
    relid = next(sh.attrib[RELS] for sh in sheets.iter(NS+"sheet") if sh.get("name") == title)
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target = next(rel.get("Target") for rel in rels if rel.get("Id") == relid)
    return target.lstrip("/") if target.startswith("/") else "xl/"+target


def edit_month_sheet(source, dest, address, expression, literal=False):
    """Edit a single cell; preserve the original workbook members and source inputs."""
    with ZipFile(source) as source_zip, ZipFile(dest, "w") as dest_zip:
        member = sheet_member(source_zip, "月结")
        for info in source_zip.infolist():
            blob = source_zip.read(info)
            if info.filename == member:
                root = ET.fromstring(blob)
                cell = next(c for c in root.iter(NS+"c") if c.get("r") == address)
                if literal:
                    for child in list(cell):
                        cell.remove(child)
                    cell.set("t", "inlineStr")
                    ET.SubElement(ET.SubElement(cell, NS+"is"), NS+"t").text = expression
                else:
                    cell.find(NS+"f").text = expression
                blob = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            dest_zip.writestr(info, blob)


def check_calculations(scorer, book_path):
    """Use known business values, independently of the reference formula interpreter."""
    book = scorer.xlsx(book_path)
    pending = "待补资料"
    cases = [
        ("原始空白", {}, [pending, 7095, pending, pending]),
        ("行政支出改为1327.25", {"明细!C2": 1327.25}, [pending, 7157.25, pending, pending]),
        ("真实零元", {"明细!C7": 0}, [17325, 7095, 10230, (17325-26000)/26000]),
        ("补入456.75元", {"明细!C7": 456.75}, [17781.75, 7095, 10686.75, (17781.75-26000)/26000]),
        ("缺金额且预算为零", {"月结!B5": 0}, [pending, 7095, pending, pending]),
        ("明细齐全且预算为零", {"明细!C7": 456.75, "月结!B5": 0}, [17781.75, 7095, 10686.75, "n.a."]),
        ("明细齐全且预算改为23000", {"明细!C7": 456.75, "月结!B5": 23000},
         [17781.75, 7095, 10686.75, (17781.75-23000)/23000]),
    ]
    complete = [1265, 2365, 3465, 4565, 5665, 456.75]
    for i, original in enumerate(complete):
        changed = complete.copy()
        changed[i] = original - 137.25
        delta = {"明细!C7": complete[-1], "明细!C" + str(i+2): changed[i]}
        cases.append((f"金额齐全后独立修改C{i+2}", delta, [sum(changed), sum(changed[:3]),
                         sum(changed[3:]), (sum(changed)-26000)/26000]))
    rows = []
    for label, inputs, expected in cases:
        output = [scorer.calc(book, "月结", cell, inputs) for cell in ("B2", "B3", "B4", "B6")]
        ok = all(math.isclose(a, b, rel_tol=0, abs_tol=1e-9)
                 if type(a) in (float, int) and type(b) in (float, int) else a == b
                 for a, b in zip(output, expected))
        rows.append(dict(场景=label, 预期=expected, 观察=output, 通过=ok))
    probe_cells = {addr: {"value": val} for addr, val in
                   {"A1": 0, "B1": True, "A2": "7", "B2": 3.5, "A3": None, "B3": "缺"}.items()}
    grid = {"格": {"cells": probe_cells}}
    for formula, expected in (("COUNT(A1:B3)", 2), ("ROWS(A1:B3)", 3),
                              ("ROWS(A3)", 1), ("COUNT(A3)", 0)):
        probe_cells["D1"] = {"formula": formula}
        value = scorer.calc(grid, "格", "D1")
        rows.append(dict(场景=formula, 预期=expected, 观察=value,
                         通过=type(value) in (int, float) and value == expected))
    return rows



def verify_monthly(root, work, reference, checked):
    task = "T053"
    results = work/"monthly-controls"
    target = results/task
    target.mkdir(parents=True)
    with ZipFile(root/"06-results/pilot_evidence.zip") as submissions:
        receipt = json.loads(submissions.read("records.json"))[task]
        for name in ("response.json", "workpaper.md", "corrected.xlsx"):
            (target/name).write_bytes(submissions.read(f"submissions/{task}/{name}"))
            checked(scorer.sha(target/name) == receipt["output_sha256"][name], f"T053/{name}: digest mismatch")
    original = work/"original.xlsx"
    shutil.copyfile(target/"corrected.xlsx", original)

    def run():
        return scorer.score(work/"受测包", work/"答案包", results, task)

    original_report = run()
    checked(original_report["auto_score"] == 70.7143 and original_report["components"]["delivery"] == 10
            and original_report["components"]["evidence_locations"] == 0.7143
            and original_report["fatal_errors"] == ["INVALID_EVIDENCE_REFERENCE"]
            and original_report["final_score"] is None, "T053: recorded workbook/evidence attribution failed")
    calculations = check_calculations(scorer, original)
    checked(all(row["通过"] for row in calculations), "Independent arithmetic oracle failed")
    total = "SUM('明细'!C2:C7)"
    missing = '"待补资料"'
    c7 = "'明细'!C7"
    tests = [
        ("空白当作零", "B2", total, False),
        ("真实零错判缺失", "B2", f'IF({c7}="",{missing},IF({c7}=0,{missing},{total}))', False),
        ("总费用写死为文字", "B2", "待补资料", True),
        ("补非零金额后仍少记该笔", "B2", f'IF({c7}="",{missing},SUM(\'明细\'!C2:C6))', False),
        ("补入金额后仍使用写死的既有费用合计", "B2", f'IF({c7}="",{missing},17325+{c7})', False),
        ("补入金额后仍使用写死的技术费用小计", "B4", f'IF({c7}="",{missing},10230+{c7})', False),
        ("行政费用公式内写死其中一笔", "B3", "SUM('明细'!C2:C3)+3465", False),
        ("预算为零时差异率错写为零", "B6", f'IF(B2={missing},{missing},IF(B5=0,0,(B2-B5)/B5))', False),
        ("差异率公式内写死预算", "B6", f'IF(B2={missing},{missing},IF(B5=0,"n.a.",(B2-26000)/26000))', False),
        ("等价但不在支持清单内", "B2", f'IF(ISNUMBER({c7}),{total},{missing})', False),
    ]

    for label, address, formula, literal in tests:
        edit_month_sheet(original, target/"corrected.xlsx", address, formula, literal)
        report = run()
        issues = [r for r in report["details"] if isinstance(r, dict) and r.get("invalid_refs")]
        checked(report["components"]["delivery"] == 5 and report["components"]["evidence_locations"] == 0.7143
                and len(issues) == 3 and report["manual_score"] is None and report["final_score"] is None, label)
        if label == "等价但不在支持清单内":
            checked(any("ISNUMBER" in row for row in report["workbook_review_required"]), label)
    shutil.copyfile(original, target/"corrected.xlsx")
    submitted = scorer.load(target/"response.json")
    expected_refs = {row["item_id"]: row["evidence_refs"] for row in scorer.load(reference/"reference_response.json")["items"]}
    for row in submitted["items"]:
        row["evidence_refs"] = expected_refs[row["item_id"]]
    (target/"response.json").write_text(json.dumps(submitted, ensure_ascii=False), encoding="utf-8")
    report = run()
    checked(report["auto_score"] == 80 and report["components"]["evidence_locations"] == 10
            and not report["fatal_errors"] and report["final_score"] is None, "T053 single-cell evidence control")
    print(f"Verified {len(calculations)} independent monthly-close oracle cases and {len(tests)+1} contrasts.")
