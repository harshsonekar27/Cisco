from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

from diagnose import diagnose_case
from review import review_diagnosis
from rule_checker import check_case

CASES_PATH = Path("data/cases.csv")
DIAGNOSES_PATH = Path("data/diagnoses.jsonl")
REVIEWS_PATH = Path("data/reviews.csv")
RESPONSIBLE_AI_PATH = Path("data/responsible_ai_log.csv")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                records.append(json.loads(line))
    return records


def save_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def upsert_diagnosis(record: dict[str, Any]) -> None:
    records = load_jsonl(DIAGNOSES_PATH)
    records = [
        item for item in records
        if item.get("case_id") != record.get("case_id")
    ]
    records.append(record)
    save_jsonl(DIAGNOSES_PATH, records)


def load_csv(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns or [])
    return pd.read_csv(path, dtype=str).fillna("")


def latest_reviews() -> pd.DataFrame:
    reviews = load_csv(
        REVIEWS_PATH,
        [
            "review_id",
            "case_id",
            "reviewer",
            "status",
            "ai_root_cause",
            "final_root_cause",
            "correction_reason",
            "reviewed_at",
        ],
    )

    if reviews.empty:
        return reviews

    return (
        reviews.sort_values("reviewed_at")
        .drop_duplicates("case_id", keep="last")
    )


def metric_percentage(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.0%"
    return f"{(numerator / denominator) * 100:.1f}%"


def render_dashboard(cases: pd.DataFrame) -> None:
    st.subheader("Project summary")

    diagnoses = pd.DataFrame(load_jsonl(DIAGNOSES_PATH))
    reviews = latest_reviews()

    reviewed_count = len(reviews)
    accepted_count = (
        int((reviews["status"] == "Accepted").sum())
        if not reviews.empty
        else 0
    )
    agreement = metric_percentage(accepted_count, reviewed_count)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Cases", len(cases))
    col2.metric("Diagnosed", len(diagnoses))
    col3.metric("Human reviewed", reviewed_count)
    col4.metric("AI-human agreement", agreement)

    left, right = st.columns(2)

    concept_counts = (
        cases.groupby("concept")
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )
    left.plotly_chart(
        px.bar(
            concept_counts,
            x="concept",
            y="count",
            title="Cases by issue type",
            labels={"concept": "Issue type", "count": "Cases"},
        ),
        use_container_width=True,
    )

    severity_order = ["Low", "Medium", "High", "Critical"]
    severity_counts = (
        cases.groupby("severity")
        .size()
        .reindex(severity_order, fill_value=0)
        .reset_index(name="count")
    )
    right.plotly_chart(
        px.bar(
            severity_counts,
            x="severity",
            y="count",
            title="Cases by severity",
            category_orders={"severity": severity_order},
            labels={"severity": "Severity", "count": "Cases"},
            color="severity",
        ),
        use_container_width=True,
    )

    if not reviews.empty:
        review_counts = reviews.groupby("status").size().reset_index(name="count")
        st.plotly_chart(
            px.pie(
                review_counts,
                names="status",
                values="count",
                title="Latest human-review outcomes",
            ),
            use_container_width=True,
        )


def render_case_workspace(cases: pd.DataFrame) -> None:
    st.subheader("Troubleshooting workspace")

    options = {
        f"{row['case_id']} - {row['title']}": row["case_id"]
        for _, row in cases.iterrows()
    }
    selected_label = st.selectbox("Select a case", list(options))
    selected_id = options[selected_label]
    case = cases[cases["case_id"] == selected_id].iloc[0].to_dict()

    st.markdown(f"**Symptom:** {case['symptom']}")
    st.markdown(f"**Topology note:** {case['topology_note']}")
    st.code(case["show_outputs"], language="text")

    st.markdown("### Deterministic checks")

    findings = check_case(case)
    if findings:
        for finding in findings:
            st.warning(
                f"{finding.severity} - {finding.message}\n\n"
                f"Evidence: {finding.evidence}\n\n"
                f"Recommendation: {finding.recommendation}"
            )
    else:
        st.info(
            "No structured rule violation was detected. "
            "The command-output evidence still requires diagnosis."
        )

    diagnoses = load_jsonl(DIAGNOSES_PATH)
    diagnosis = next(
        (item for item in diagnoses if item.get("case_id") == selected_id),
        None,
    )

    api_available = bool(load_dotenv() or True)
    use_api = st.checkbox(
        "Use configured AI API",
        value=False,
        help="Without an API key, the application uses its local demonstration fallback.",
    )

    if st.button("Run diagnosis", type="primary"):
        try:
            diagnosis = diagnose_case(case, use_api=use_api)
            upsert_diagnosis(diagnosis)
            st.success("Diagnosis created. It remains pending human review.")
            st.rerun()
        except Exception as exc:
            st.error(f"Diagnosis failed: {exc}")

    if diagnosis is None:
        return

    st.markdown("### AI recommendation")

    status = diagnosis.get("review_status", "Pending")
    if status == "Pending":
        st.warning("Pending human review. The proposed fix is not accepted.")
    elif status == "Rejected":
        st.error("The human reviewer rejected this diagnosis.")
    else:
        st.success(f"Human review status: {status}")

    display_diagnosis = {
        key: value
        for key, value in diagnosis.items()
        if key not in {"rule_findings"}
    }
    st.json(display_diagnosis)

    st.markdown("### Human review")

    with st.form(f"review-{selected_id}"):
        reviewer = st.text_input("Reviewer name")
        review_status = st.selectbox(
            "Decision",
            ["Accepted", "Edited", "Rejected"],
        )
        final_root_cause = st.text_area(
            "Final root cause",
            value=diagnosis.get("root_cause", ""),
        )
        reason = st.text_area(
            "Correction reason",
            help="Required when the diagnosis is edited or rejected.",
        )
        risk = st.text_area(
            "Risk if the AI answer were used without review",
            value=(
                "An incorrect configuration change could cause downtime, "
                "hide the root cause, or weaken security."
            ),
        )
        submitted = st.form_submit_button("Save human review")

    if submitted:
        try:
            if not reviewer.strip():
                raise ValueError("Reviewer name is required.")

            review_diagnosis(
                case_id=selected_id,
                reviewer=reviewer,
                status=review_status,
                final_root_cause=final_root_cause,
                reason=reason,
                risk=risk,
            )
            st.success("Human review saved.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    st.markdown("### Fix acceptance gate")

    current_records = load_jsonl(DIAGNOSES_PATH)
    current = next(
        (item for item in current_records if item.get("case_id") == selected_id),
        diagnosis,
    )

    review_complete = current.get("review_status") in {"Accepted", "Edited"}
    st.button(
        "Accept fix for lab implementation",
        disabled=not review_complete,
        help=(
            "This button remains disabled until a human accepts or edits "
            "the diagnosis."
        ),
    )

    if review_complete:
        st.info(
            "The diagnosis has human approval. Apply the reviewed change in "
            "Packet Tracer, then execute the verification steps."
        )


def render_responsible_ai_log() -> None:
    st.subheader("Responsible AI correction log")

    log = load_csv(
        RESPONSIBLE_AI_PATH,
       
    )