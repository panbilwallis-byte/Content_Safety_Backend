"""Compare Task 2 submissions by trajectory ID without echoing source text."""
import argparse
import json
from pathlib import Path


def read_rows(path):
    """Accept JSONL files and the current competition-report API envelope."""
    content = path.read_text(encoding="utf-8-sig")
    try:
        document = json.loads(content)
    except json.JSONDecodeError:
        document = None
    if isinstance(document, dict) and "data" in document and "trajectory_id" not in document:
        document = document["data"]
    if isinstance(document, dict) and isinstance(document.get("jsonl"), str):
        content = document["jsonl"]
        document = None
    if isinstance(document, dict) and "trajectory_id" in document:
        source_rows = [(1, document)]
    elif isinstance(document, list):
        source_rows = list(enumerate(document, 1))
    else:
        source_rows = [(i, json.loads(line)) for i, line in enumerate(content.splitlines(), 1) if line.strip()]
    rows = {}
    for line_number, row in source_rows:
        if not isinstance(row, dict) or not isinstance(row.get("trajectory_id"), str) or not row["trajectory_id"]:
            raise ValueError(f"{path.name} 第 {line_number} 条缺少有效 trajectory_id")
        identity = row["trajectory_id"]
        if identity in rows:
            raise ValueError(f"{path.name} 第 {line_number} 行重复轨迹 {identity}")
        if not isinstance(row.get("results"), dict):
            raise ValueError(f"{path.name} 第 {line_number} 条 results 必须是对象")
        rows[identity] = row["results"]
    return rows


def locations(rows):
    return [(row.get("step_id"), row.get("message_index"), row.get("field")) for row in rows]


def call_content(rows):
    return [(row["step_id"], row["tool_name"], row["arguments"], [v["content"] for v in row["returns"]])
            for row in rows]


def comparable(code, result):
    if code == "B3":
        return [row["step_id"] for row in result["risks"]]
    if code == "C1":
        return call_content(result["calls"]), result["unmatched"]
    if code == "C2":
        return [(row["step_id"], row["previous_step_id"]) for row in result["markers"]]
    if code == "C3":
        return [(row["step_id"], row["error_type"]) for row in result["failures"]]
    if code == "C4":
        return result["completion"], result["missing_fields"], result["unmatched"], locations(result["truncations"])
    if code == "C5":
        return [(row["source_subitem"], *position) for row, position in zip(result["issues"], locations(result["issues"]))]
    return result


def compare(actual, expected):
    differences = [f"缺少轨迹 {identity}" for identity in sorted(expected.keys() - actual.keys())]
    differences += [f"多余轨迹 {identity}" for identity in sorted(actual.keys() - expected.keys())]
    for identity, target in expected.items():
        if identity not in actual:
            continue
        result = actual[identity]
        for code in sorted(result.keys() | target.keys()):
            if code not in result or code not in target:
                differences.append(f"{identity} {code} 字段缺失或多余")
                continue
            try:
                different = (result[code].get("指标") != target[code].get("指标")
                             or comparable(code, result[code]) != comparable(code, target[code]))
            except (KeyError, TypeError, AttributeError):
                differences.append(f"{identity} {code} 格式不完整")
            else:
                if different:
                    differences.append(f"{identity} {code} 与参考结果不同")
    return differences


def main():
    parser = argparse.ArgumentParser(description="自动比对赛题二结构化结果。证据引用是否充分仍需复核。")
    parser.add_argument("submission", type=Path)
    parser.add_argument("reference", type=Path)
    args = parser.parse_args()
    try:
        differences = compare(read_rows(args.submission), read_rows(args.reference))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"无法读取结果文件：{error}\n")
    for message in differences:
        print(message)
    print(f"发现 {len(differences)} 项差异" if differences else "可自动比对的指标全部一致")
    return int(bool(differences))


if __name__ == "__main__":
    raise SystemExit(main())
