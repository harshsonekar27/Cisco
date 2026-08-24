from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIAGNOSES_PATH = Path("data/diagnoses.jsonl")
REVIEWS_PATH = Path("data/reviews.csv")
RESPONSIBLE_AI_PATH = Path("data/responsible_ai_log.csv")

REVIEW_FIELDS = [
    "review_id",
    "case_id",
    "reviewer",
    "status",
    "ai_root_cause",
    "final_root_cause",
    "correction_reason",
    "reviewed_at",
]

RESPONSIBLE_AI_FIELDS = [
    "case_id",
    "ai_answer",
    "human_correction",
    "why_ai_was_wrong",
    "risk_if_unreviewed",
    "reviewer",
    "reviewed_at",
]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist. Run diagnose.py first.")

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                records.append(json.loads(line))
    return records


def save_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_csv(path: Path, fields: list[str], row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0

    with path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def review_diagnosis(
    case_id: str,
    reviewer: str,
    status: str,
    final_root_cause: str,
    reason: str,
    risk: str,
) -> None:
    diagnoses = load_jsonl(DIAGNOSES_PATH)
    selected = next(
        (item for item in diagnoses if item.get("case_id") == case_id),
        None,
    )

    if selected is None:
        raise ValueError(f"Diagnosis for {case_id} was not found.")

    status = status.title()
    if status not in {"Accepted", "Edited", "Rejected"}:
        raise ValueError("Status must be Accepted, Edited, or Rejected.")

    ai_root_cause = str(selected.get("root_cause", "")).strip()

    if status == "Accepted":
        final_root_cause = ai_root_cause
    elif not final_root_cause.strip():
        raise ValueError("Edited or Rejected reviews require a final root cause.")

    if status in {"Edited", "Rejected"} and not reason.strip():
        raise ValueError("Edited or Rejected reviews require a correction reason.")

    reviewed_at = datetime.now(timezone.utc).isoformat()
    review_id = f"{case_id}-{reviewed_at}"

    selected["review_status"] = status
    selected["reviewer"] = reviewer
    selected["reviewed_at"] = reviewed_at
    selected["human_root_cause"] = final_root_cause
    selected["correction_reason"] = reason
    selected["accepted_fix"] = status in {"Accepted", "Edited"}

    save_jsonl(DIAGNOSES_PATH, diagnoses)

    append_csv(
        REVIEWS_PATH,
        REVIEW_FIELDS,
        {
            "review_id": review_id,
            "case_id": case_id,
            "reviewer": reviewer,
            "status": status,
            "ai_root_cause": ai_root_cause,
            "final_root_cause": final_root_cause,
            "correction_reason": reason,
            "reviewed_at": reviewed_at,
        },
    )

    if status in {"Edited", "Rejected"}:
        append_csv(
            RESPONSIBLE_AI_PATH,
            RESPONSIBLE_AI_FIELDS,
            {
                "case_id": case_id,
                "ai_answer": ai_root_cause,
                "human_correction": final_root_cause,
                "why_ai_was_wrong": reason,
                "risk_if_unreviewed": risk,
                "reviewer": reviewer,
                "reviewed_at": reviewed_at,
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Review a NetSage diagnosis.")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument(
        "--status",
        required=True,
        choices=["Accepted", "Edited", "Rejected"],
    )
    parser.add_argument("--final-root-cause", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument(
        "--risk",
        default="An incorrect change could cause downtime or weaken security.",
    )
    args = parser.parse_args()

    review_diagnosis(
        case_id=args.case_id,
        reviewer=args.reviewer,
        status=args.status,
        final_root_cause=args.final_root_cause,
        reason=args.reason,
        risk=args.risk,
    )

    print(f"{args.case_id} marked {args.status} by {args.reviewer}.")


if __name__ == "__main__":
    main()
