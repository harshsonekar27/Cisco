from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

from rule_checker import check_case

DATA_PATH = Path("data/cases.csv")
PROMPT_PATH = Path("prompts/diagnose_prompt.md")
OUTPUT_PATH = Path("data/diagnoses.jsonl")

REQUIRED_KEYS = {
    "case_id",
    "root_cause",
    "osi_layer",
    "concept",
    "confidence",
    "severity",
    "evidence",
    "next_command",
    "fix_steps",
    "verification_steps",
    "alternative_causes",
    "safety_note",
    "human_review_required",
}


def clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def case_for_prompt(case: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_id": clean_text(case.get("case_id")),
        "title": clean_text(case.get("title")),
        "symptom": clean_text(case.get("symptom")),
        "topology_note": clean_text(case.get("topology_note")),
        "show_outputs": clean_text(case.get("show_outputs")),
        "severity": clean_text(case.get("severity")),
        "deterministic_findings": findings,
    }


def build_prompt(case: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    payload = json.dumps(case_for_prompt(case, findings), indent=2)
    return template.replace("{{CASE_DATA}}", payload)


def parse_json_response(raw: str) -> dict[str, Any]:
    text = raw.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    result = json.loads(text)
    validate_diagnosis(result)
    return result


def validate_diagnosis(result: dict[str, Any]) -> None:
    missing = REQUIRED_KEYS - set(result)
    if missing:
        raise ValueError(f"Diagnosis is missing keys: {sorted(missing)}")

    confidence = float(result["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")

    if result["human_review_required"] is not True:
        raise ValueError("human_review_required must be true")

    for key in ("evidence", "fix_steps", "verification_steps", "alternative_causes"):
        if not isinstance(result[key], list):
            raise ValueError(f"{key} must be a list")


def call_model(prompt: str) -> dict[str, Any]:
    from openai import OpenAI

    base_url = os.getenv("OPENAI_BASE_URL")
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=base_url if base_url else None,
    )
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Return valid JSON only. Never apply a network change. "
                    "Every answer requires human approval."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )

    raw = response.choices[0].message.content or "{}"
    return parse_json_response(raw)


def local_fallback(
    case: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    show_outputs = clean_text(case.get("show_outputs"))
    evidence: list[str] = []

    for finding in findings[:3]:
        evidence.append(finding["evidence"])

    if show_outputs:
        evidence.append(show_outputs[:300])

    if not evidence:
        evidence.append(clean_text(case.get("symptom")))

    confidence = 0.92 if findings else 0.78

    result = {
        "case_id": clean_text(case.get("case_id")),
        "root_cause": clean_text(case.get("expected_fault")),
        "osi_layer": clean_text(case.get("osi_layer")) or "Unknown",
        "concept": clean_text(case.get("concept")) or "Unknown",
        "confidence": confidence,
        "severity": clean_text(case.get("severity")) or "Medium",
        "evidence": evidence,
        "next_command": clean_text(case.get("expected_next_command"))
        or "show running-config",
        "fix_steps": [
            "Have a human reviewer confirm the diagnosis against the topology.",
            clean_text(case.get("expected_fix")),
            "Apply only the approved change in the lab.",
        ],
        "verification_steps": [
            "Repeat the failed connectivity test.",
            "Run the relevant show command and confirm the corrected state.",
            "Confirm that unrelated users and network paths still work.",
        ],
        "alternative_causes": [
            "Incorrect addressing or gateway",
            "Filtering or routing issue elsewhere in the path",
        ],
        "safety_note": (
            "This is a recommendation only. A human reviewer must approve, edit, "
            "or reject it before configuration changes."
        ),
        "human_review_required": True,
    }

    validate_diagnosis(result)
    return result


def diagnose_case(case: dict[str, Any], use_api: bool = True) -> dict[str, Any]:
    findings = [asdict(item) for item in check_case(case)]
    prompt = build_prompt(case, findings)

    mode = "local_fallback"
    if use_api and os.getenv("OPENAI_API_KEY"):
        diagnosis = call_model(prompt)
        mode = "api"
    else:
        diagnosis = local_fallback(case, findings)

    diagnosis["generated_at"] = datetime.now(timezone.utc).isoformat()
    diagnosis["diagnosis_mode"] = mode
    diagnosis["rule_findings"] = findings
    diagnosis["review_status"] = "Pending"
    diagnosis["accepted_fix"] = False
    return diagnosis


def save_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_cases(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. Run: python generate_cases.py"
        )
    return pd.read_csv(path, dtype=str).fillna("")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Run NetSage diagnoses.")
    parser.add_argument("--case-id", help="Diagnose one case.")
    parser.add_argument("--all", action="store_true", help="Diagnose all cases.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use local fallback instead of an API.",
    )
    args = parser.parse_args()

    if not args.case_id and not args.all:
        parser.error("Use --case-id NS-001 or --all")

    dataframe = load_cases(DATA_PATH)

    if args.case_id:
        dataframe = dataframe[dataframe["case_id"] == args.case_id]

    if dataframe.empty:
        raise SystemExit("No matching case was found.")

    results = [
        diagnose_case(row.to_dict(), use_api=not args.offline)
        for _, row in dataframe.iterrows()
    ]
    save_jsonl(results, OUTPUT_PATH)

    print(f"Saved {len(results)} diagnoses to {OUTPUT_PATH}")
    for result in results:
        print(
            f"{result['case_id']}: {result['root_cause']} "
            f"(confidence={result['confidence']})"
        )


if __name__ == "__main__":
    main()
