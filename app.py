import json
import hmac
from pathlib import Path
from typing import Any

import streamlit as st

import db
from config import get_setting
from pipeline import run_pipeline
from resubmission import simulate_vendor_resubmission
from submission import SubmissionJSONError, parse_submission_json


ROOT = Path(__file__).parent
SAMPLES_DIR = ROOT / "data" / "samples"
DOCUMENT_OPTIONS = [
    "incorporation_cert",
    "tax_registration",
    "bank_proof",
    "w9_or_w8",
]
COUNTRIES = ["IN", "US", "GB", "DE"]
STATUS_STYLES = {
    "APPROVED": ("#15803d", "#f0fdf4"),
    "PENDING": ("#a16207", "#fffbeb"),
    "REJECTED": ("#b91c1c", "#fef2f2"),
}
STAGE_ORDER = [
    "Intake & parsing",
    "Completeness & documents",
    "Format validation",
    "Cross-reference",
    "Risk screening",
    "Decision",
    "Vendor communication",
]


def _require_login() -> None:
    configured_password = get_setting("APP_PASSWORD")
    if not configured_password:
        st.error(
            "App access is locked. Configure APP_PASSWORD in your environment, "
            ".env file, or Streamlit secrets before starting the app."
        )
        st.stop()

    if st.session_state.get("authenticated"):
        if st.sidebar.button("Log out"):
            st.session_state["authenticated"] = False
            st.rerun()
        return

    st.title("Vendor onboarding")
    st.info("Enter the configured app password to continue.")
    with st.form("app_login"):
        password = st.text_input("App password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if hmac.compare_digest(
            password.encode("utf-8"), configured_password.encode("utf-8")
        ):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("That password wasn't accepted.")
    st.stop()


def _read_sample(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as sample_file:
        submission: Any = json.load(sample_file)
    if not isinstance(submission, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return submission


def _load_submission_from_form() -> dict[str, Any]:
    return {
        "company_name": st.text_input("Company name", key="form_company_name"),
        "country": st.selectbox("Country", COUNTRIES, key="form_country"),
        "tax_id": st.text_input("Tax ID", key="form_tax_id"),
        "registered_address": st.text_area(
            "Registered address", key="form_registered_address"
        ),
        "contact_name": st.text_input("Contact name", key="form_contact_name"),
        "contact_email": st.text_input("Contact email", key="form_contact_email"),
        "bank_account_holder": st.text_input(
            "Bank account holder", key="form_bank_account_holder"
        ),
        "bank_name": st.text_input("Bank name", key="form_bank_name"),
        "bank_account_number": st.text_input(
            "Bank account number / IBAN", key="form_bank_account_number"
        ),
        "bank_country": st.selectbox(
            "Bank country", COUNTRIES, key="form_bank_country"
        ),
        "ifsc_or_routing": st.text_input(
            "IFSC / routing details", key="form_ifsc_or_routing"
        ),
        "documents": st.multiselect(
            "Documents provided",
            DOCUMENT_OPTIONS,
            key="form_documents",
        ),
    }


def _stage_state(results: list[dict[str, Any]]) -> tuple[str, str]:
    severities = {result.get("severity") for result in results}
    if "reject" in severities:
        return "error", "❌"
    if "pending" in severities:
        return "complete", "⚠️"
    return "complete", "✅"


def _render_rule_results(results: list[dict[str, Any]]) -> None:
    if not results:
        st.caption("No rule findings for this stage.")
        return
    for result in results:
        severity = result.get("severity", "ok")
        symbol = {"ok": "✓", "pending": "!", "reject": "×"}.get(
            severity, "•"
        )
        st.markdown(
            f"**{symbol} {result.get('rule', 'Rule')}** — "
            f"{result.get('message', '')}"
        )


def _render_stage_history(events: list[dict[str, Any]]) -> None:
    completed = {
        event["stage"]: event
        for event in events
        if event.get("status") == "done"
    }
    for stage in STAGE_ORDER:
        event = completed.get(stage)
        if event is None:
            continue
        state, icon = _stage_state(event.get("results", []))
        with st.status(
            f"{icon} {stage} · {event.get('elapsed_ms', 0)} ms",
            state=state,
            expanded=False,
        ):
            _render_rule_results(event.get("results", []))


def _render_final(final: dict[str, Any]) -> bool:
    status = final.get("status", "PENDING")
    foreground, background = STATUS_STYLES.get(
        status, STATUS_STYLES["PENDING"]
    )
    st.markdown(
        f"""
        <div style="
            display:inline-block;
            padding:0.55rem 1rem;
            margin:0.65rem 0;
            border-radius:999px;
            color:{foreground};
            background:{background};
            border:1px solid {foreground};
            font-size:1.15rem;
            font-weight:700;">
            {status}
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(final.get("decision_summary", ""))

    all_rules = [
        result
        for event in st.session_state.get("run_events", [])
        if event.get("status") == "done"
        and event.get("stage") not in {"Decision", "Vendor communication"}
        for result in event.get("results", [])
        if result.get("rule") != "intake"
    ]
    if all_rules:
        st.subheader("Rule-by-rule results")
        st.dataframe(
            [
                {
                    "rule": result.get("rule", ""),
                    "result": result.get("severity", "").upper(),
                    "message": result.get("message", ""),
                }
                for result in all_rules
            ],
            hide_index=True,
            width="stretch",
        )

    email_draft = final.get("email_draft")
    if status != "APPROVED" and email_draft:
        st.subheader("Vendor email draft")
        st.text_area(
            "Copy or edit this draft",
            value=email_draft,
            height=220,
            key=f"result_email_draft_{st.session_state.run_generation}",
        )
        st.code(email_draft, language=None)
    if status == "PENDING" and final.get("run_id") is not None:
        return st.button(
            "Simulate vendor resubmission",
            key=f"simulate_resubmission_{final['run_id']}",
            help="Fills the missing demo information and links the new run to this one.",
        )
    return False


def _execute_pipeline(
    submission: dict[str, Any], parent_run_id: int | None = None
) -> None:
    st.session_state.run_generation += 1
    st.session_state.run_events = []
    st.session_state.run_final = None
    st.session_state.run_submission = submission
    stage_statuses = {}
    for event in run_pipeline(submission, parent_run_id=parent_run_id):
        st.session_state.run_events.append(event)
        stage = event["stage"]
        if event["status"] == "running":
            stage_statuses[stage] = st.status(
                f"⏳ {stage}",
                state="running",
                expanded=True,
            )
        else:
            status_widget = stage_statuses[stage]
            with status_widget:
                _render_rule_results(event.get("results", []))
            state, icon = _stage_state(event.get("results", []))
            status_widget.update(
                label=f"{icon} {stage} · {event.get('elapsed_ms', 0)} ms",
                state=state,
                expanded=False,
            )
        if "final" in event:
            st.session_state.run_final = event["final"]


st.set_page_config(page_title="Vendor onboarding", layout="wide")
_require_login()
db.init_db()

st.markdown(
    """
    <style>
    .block-container {padding-top: 1.6rem; padding-bottom: 2rem;}
    [data-testid="stStatusWidget"] {border-radius: 0.6rem;}
    div[data-testid="stVerticalBlock"] > div:has(> div > .stage-marker) {
        gap: 0.35rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Vendor onboarding")
st.caption("From submission to approval · Procurement review workspace")
run_tab, dashboard_tab, assumptions_tab = st.tabs(
    ["Run", "Dashboard", "Assumptions"]
)

if "run_events" not in st.session_state:
    st.session_state.run_events = []
if "run_final" not in st.session_state:
    st.session_state.run_final = None
if "run_submission" not in st.session_state:
    st.session_state.run_submission = None
if "run_generation" not in st.session_state:
    st.session_state.run_generation = 0

with run_tab:
    input_column, live_column = st.columns([0.95, 1.25], gap="large")
    with input_column:
        st.subheader("Submission")
        input_mode = st.radio(
            "Input source",
            ["Load sample", "Fill form", "Upload JSON"],
            horizontal=True,
            label_visibility="collapsed",
        )

        submission: dict[str, Any] | None = None
        if input_mode == "Load sample":
            sample_files = sorted(SAMPLES_DIR.glob("*.json"))
            if not sample_files:
                st.error("No sample submissions found in data/samples.")
            else:
                selected_sample = st.selectbox(
                    "Sample submission",
                    sample_files,
                    format_func=lambda path: path.stem.replace("_", " ").title(),
                )
                try:
                    submission = _read_sample(selected_sample)
                except (OSError, json.JSONDecodeError, ValueError) as error:
                    st.error(f"Could not load sample: {error}")
        elif input_mode == "Fill form":
            submission = _load_submission_from_form()
        else:
            uploaded_file = st.file_uploader(
                "Choose a JSON submission",
                type=["json"],
                accept_multiple_files=False,
            )
            if uploaded_file is not None:
                try:
                    submission = parse_submission_json(uploaded_file.getvalue())
                except SubmissionJSONError as error:
                    st.error("We couldn't read that file as a vendor submission.")
                    st.caption(str(error))

        if submission is not None:
            with st.expander("View submission JSON", expanded=False):
                st.json(submission)
            run_clicked = st.button(
                "Run onboarding check",
                type="primary",
                width="stretch",
            )
        else:
            run_clicked = False

    with live_column:
        st.subheader("Live run view")
        if run_clicked and submission is not None:
            _execute_pipeline(submission)
            if st.session_state.run_final is not None:
                resubmit_clicked = _render_final(st.session_state.run_final)
            else:
                resubmit_clicked = False
        elif st.session_state.run_events:
            _render_stage_history(st.session_state.run_events)
            if st.session_state.run_final:
                resubmit_clicked = _render_final(st.session_state.run_final)
            else:
                resubmit_clicked = False
        else:
            st.info("Choose a submission and run the onboarding checks to begin.")
            resubmit_clicked = False

        if resubmit_clicked and st.session_state.run_final:
            previous_final = st.session_state.run_final
            previous_submission = st.session_state.run_submission
            if previous_submission is not None:
                corrected, changes = simulate_vendor_resubmission(
                    previous_submission, previous_final.get("reasons", [])
                )
                if changes:
                    st.info("Simulated vendor updates:\n\n" + "\n".join(
                        f"- {change}" for change in changes
                    ))
                    _execute_pipeline(
                        corrected,
                        parent_run_id=previous_final.get("run_id"),
                    )
                    if st.session_state.run_final is not None:
                        _render_final(st.session_state.run_final)

with dashboard_tab:
    st.subheader("Portfolio overview")
    runs = db.list_runs()
    status_counts = {
        status: sum(run["status"] == status for run in runs)
        for status in ("APPROVED", "PENDING", "REJECTED")
    }
    total_runs = len(runs)
    auto_approved = (
        status_counts["APPROVED"] / total_runs * 100 if total_runs else 0
    )
    metrics = st.columns(5)
    metrics[0].metric("Total runs", total_runs)
    metrics[1].metric("Approved", status_counts["APPROVED"])
    metrics[2].metric("Pending", status_counts["PENDING"])
    metrics[3].metric("Rejected", status_counts["REJECTED"])
    metrics[4].metric("Auto-approved", f"{auto_approved:.1f}%")

    chart_column, demo_column = st.columns([1.3, 1], gap="large")
    with chart_column:
        st.markdown("#### Status distribution")
        st.bar_chart(
            {
                "Status": ["APPROVED", "PENDING", "REJECTED"],
                "Runs": [
                    status_counts["APPROVED"],
                    status_counts["PENDING"],
                    status_counts["REJECTED"],
                ],
            },
            x="Status",
            y="Runs",
            x_label="Status",
            y_label="Runs",
        )
    with demo_column:
        st.markdown("#### Demo controls")
        if st.button("Run all samples", type="primary", width="stretch"):
            sample_files = sorted(SAMPLES_DIR.glob("*.json"))
            if not sample_files:
                st.warning("No sample submissions found in data/samples.")
            else:
                progress = st.progress(0, text="Running sample submissions")
                for index, sample_path in enumerate(sample_files, start=1):
                    try:
                        sample_submission = _read_sample(sample_path)
                        for _event in run_pipeline(sample_submission):
                            pass
                    except (OSError, json.JSONDecodeError, ValueError) as error:
                        st.error(f"Could not run {sample_path.name}: {error}")
                    progress.progress(
                        index / len(sample_files),
                        text=f"Completed {sample_path.name}",
                    )
                st.success(f"Processed {len(sample_files)} sample submissions.")
                st.rerun()

    st.markdown("#### Run history")
    status_filter = st.multiselect(
        "Filter by status",
        ["APPROVED", "PENDING", "REJECTED"],
        default=["APPROVED", "PENDING", "REJECTED"],
    )
    search_text = st.text_input("Search vendor name or summary")

    run_summaries: dict[int, str] = {}
    for run in runs:
        reasons = json.loads(run["reasons_json"])
        actionable = [
            reason for reason in reasons if reason.get("severity") != "ok"
        ]
        run_summaries[run["id"]] = "; ".join(
            str(reason.get("message", "")) for reason in actionable
        ) or "All checks passed."

    query = search_text.casefold().strip()
    filtered_runs = [
        run
        for run in runs
        if run["status"] in status_filter
        and (
            not query
            or query in str(run["vendor_name"]).casefold()
            or query in run_summaries[run["id"]].casefold()
        )
    ]
    st.dataframe(
        [
            {
                "Timestamp": run["timestamp"],
                "Vendor": run["vendor_name"],
                "Status": run["status"],
                "Summary": run_summaries[run["id"]],
                "Parent run": run["parent_run_id"],
            }
            for run in filtered_runs
        ],
        hide_index=True,
        width="stretch",
    )

    if runs:
        run_by_id = {run["id"]: run for run in runs}
        selected_run_id = st.selectbox(
            "Inspect run",
            list(run_by_id),
            format_func=lambda run_id: (
                f"Run #{run_id} · {run_by_id[run_id]['vendor_name']} · "
                f"{run_by_id[run_id]['status']}"
            ),
        )
        selected_run = run_by_id[selected_run_id]
        selected_reasons = json.loads(selected_run["reasons_json"])
        selected_submission = json.loads(selected_run["submission_json"])
        st.markdown(
            f"#### Run #{selected_run_id}: {selected_run['vendor_name']} "
            f"— {selected_run['status']}"
        )
        st.caption(selected_run["timestamp"])
        if selected_run["parent_run_id"] is not None:
            st.caption(f"Resubmission of run #{selected_run['parent_run_id']}")
        st.markdown(f"**Summary:** {run_summaries[selected_run_id]}")
        if selected_reasons:
            st.dataframe(
                [
                    {
                        "Rule": reason.get("rule", ""),
                        "Result": reason.get("severity", "").upper(),
                        "Message": reason.get("message", ""),
                    }
                    for reason in selected_reasons
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.success("All checks passed.")
        if selected_run["email_draft"]:
            st.markdown("#### Vendor email draft")
            st.text_area(
                "Email draft",
                value=selected_run["email_draft"],
                height=200,
                key=f"dashboard_email_{selected_run_id}",
            )
        with st.expander("Submitted data"):
            st.json(selected_submission)
    else:
        st.info("Completed onboarding runs will appear here.")

    with st.sidebar.expander("History maintenance", expanded=False):
        st.warning("Clearing history permanently removes all saved runs.")
        if st.button("Clear history", type="secondary"):
            db.clear_runs()
            st.session_state.run_events = []
            st.session_state.run_final = None
            st.session_state.run_submission = None
            st.success("Run history cleared.")
            st.rerun()

with assumptions_tab:
    st.subheader("Decision assumptions")
    st.markdown(
        """
        - **Supported countries:** India (IN), United States (US), United
          Kingdom (GB), and Germany (DE).
        - **Required documents:**
          - IN, GB, and DE: incorporation certificate, tax registration, and
            bank proof.
          - US: W-9 or W-8 and bank proof.
        - **REJECTED:** used for hard failures that block onboarding, including
          malformed country-specific tax IDs, bank-country or bank-account
          format mismatches, confirmed duplicate tax IDs or account numbers,
          and very low-confidence matches between substantially different
          company and bank-holder names.
        - **PENDING:** used when required fields or documents are missing, a
          corporate email is needed instead of a flagged free email, or an
          uncertain company/bank-holder name needs clarification. Pending
          means more information or manual review is needed, not that fraud was
          established.
        - **Duplicate detection:** submitted tax IDs and bank account numbers
          are normalized and compared with the local approved-vendor master.
          A match is rejected when it belongs to a differently named company.
        - **LLM use:** the LLM is limited to fuzzy name matching and drafting
          vendor follow-up emails. Format, completeness, document,
          cross-reference, and duplicate rules are deterministic Python checks.
        - **Out of scope:** tax IDs and bank details are format-checked but are
          not verified against real government or banking registries. A next
          production step would be secure integrations with authoritative tax
          and account-verification services, with audit logging and manual
          review for unavailable or inconclusive results.
        """
    )
