# End-to-end workflow

The app accepts a vendor submission through the Run tab, passes the resulting Python dictionary to a synchronous generator, and evaluates eight rule functions against the bundled approved-vendor master. Severity determines the decision; non-approved outcomes get an email draft. Completed runs are written to SQLite, and the Dashboard reads that history for metrics and review. For reproducibility, the examples below describe the current code path and its fallback behavior, not an external registry check. (`app.py:282-369`; `pipeline.py:66-163`; `rules.py:433-466`; `db.py:29-123`)

```text
Run UI / sample / form / upload
              |
              v
       app.py input loaders
              |
       uploaded JSON parser
              |
              v
    pipeline.run_pipeline(dict)
      |       |        |       |
      v       v        v       v
 Completeness Format Cross-ref Risk screening
   & docs    rules   rules       rules
                      |            |
                      +--> llm.py <-+
              |
              v
      Decision (reject > pending > approved)
              |
       +------+------+
       |             |
   APPROVED       otherwise
       |             v
       |      Vendor email draft
       +-------> SQLite DB
                    |
                    v
            Dashboard / history
```

The UI renders generator events as they arrive, and both approved and non-approved outcomes are saved before the final event is yielded. The Vendor communication stage is skipped for an approved submission. (`app.py:219-247`; `pipeline.py:27-47`; `pipeline.py:114-162`)

## Layers and stages, in order

### Input loading and JSON parsing

| Where | Data IN -> exact data OUT | What happens / side effects |
|---|---|---|
| Sample loader: `app.py:_read_sample` and Run-tab sample branch (`app.py:71-77`; `app.py:282-310`) | Path to a `.json` file -> `dict[str, Any]`, e.g. `{"company_name":"Blue Peak Logistics Pvt Ltd","country":"IN",...}`. The file is decoded as UTF-8 JSON and must be an object. | Reads from `data/samples`; catches file, JSON, and object-shape errors in the UI and shows an error. Loading itself does not invoke the pipeline or LLM; `db.init_db()` also runs separately at app startup. (`app.py:250-252`; `app.py:282-310`) |
| Form loader: `app.py:_load_submission_from_form` (`app.py:79-108`; `app.py:282-336`) | Streamlit widget values -> a dict with string values for `company_name`, `country`, `tax_id`, `registered_address`, `contact_name`, `contact_email`, `bank_account_holder`, `bank_name`, `bank_account_number`, `bank_country`, and `ifsc_or_routing`; `documents` is a list of strings. | Renders text inputs, two country selectors, and a document multiselect. It does not call the JSON parser or enforce the schema at this point. The loader itself does not call the DB or LLM; app startup initializes the DB separately. (`app.py:79-108`; `app.py:250-252`) |
| Upload loader: Run-tab upload branch (`app.py:311-323`) | Uploaded bytes -> the same submission dict shape, or a displayed friendly error. | Calls `parse_submission_json`; malformed uploads do not enter the pipeline. |
| JSON parser: `submission.py:SubmissionJSONError`, `parse_submission_json` (`submission.py:5-26`) | UTF-8 bytes -> `dict[str, Any]`; example: `b'{"company_name":"Example"}'` -> `{"company_name":"Example"}`. | Rejects invalid UTF-8/JSON, non-object JSON, a non-string `country`, non-list `documents`, or non-string document entries. It does **not** enforce all required fields, country enums, or the complete schema; rules handle missing/invalid submission values later. No DB or LLM effects. |

The submission has 12 expected fields; `documents` is the only list-valued field in the form. Missing keys are allowed through JSON parsing and become rule findings rather than parse errors. (`app.py:17-23`; `app.py:79-108`; `submission.py:9-26`; `rules.py:19-32`; `rules.py:119-176`)

### Pipeline intake and progress-event contract

`pipeline.run_pipeline(submission, parent_run_id=None)` accepts a dict and yields event dictionaries. A non-dict raises `TypeError`. Before yielding intake progress it initializes SQLite and loads `data/vendors_master.json`; if the master file is absent, it uses an empty list. (`pipeline.py:66-75`; `pipeline.py:165-173`)

Every ordinary progress event has exactly this shape:

```python
{
    "stage": str,
    "status": "running" | "done",
    "results": list[RuleResult],
    "elapsed_ms": int,
}
# RuleResult = {"rule": str, "passed": bool,
#               "severity": "ok" | "pending" | "reject",
#               "message": str}
```

The final event also has a `final` key. The 0.4-second sleep occurs after each running event; elapsed time measures from that stage’s start. (`pipeline.py:27-47`; `pipeline.py:77-98`; `rules.py:12-17`)

### Seven stages

Each rule stage receives the original submission dict and the loaded master-vendor list. Rule functions return `RuleResult` dictionaries; the stage wraps them in the progress-event shape above. Rules do not modify the submission. The stage list and rule membership are defined in `pipeline.py`. (`pipeline.py:27-33`; `pipeline.py:88-94`; `rules.py:12-17`)

| Stage | Where / rule functions | Data IN -> OUT example | What it does; side effects |
|---|---|---|---|
| 1. Intake & parsing | `pipeline.run_pipeline` (`pipeline.py:76-86`) | Dict + master list -> `{"stage":"Intake & parsing","status":"done","results":[{"rule":"intake","passed":true,"severity":"ok","message":"Submission JSON object received."}],"elapsed_ms":int}` | Confirms the input is a dict (not that every field is valid); yields running/done events and sleeps 0.4 s. The JSON parsing itself happened earlier, only for uploads. |
| 2. Completeness & documents | `completeness`, `required_documents` (`pipeline.py:29-30`; `rules.py:119-209`) | Submission dict + master list -> stage event with two `RuleResult`s; e.g. a missing `bank_proof` yields `required_documents` with `passed:false`, `severity:"pending"`. | Checks required fields, types, lengths, unknown keys, and country-required documents. Yields results and sleeps 0.4 s; no DB write here. |
| 3. Format validation | `tax_id_format`, `bank_account_format` (`pipeline.py:30-30`; `rules.py:210-295`) | Same input -> results for `tax_id_format` and `bank_account_format`; invalid country-specific format yields `severity:"reject"`. | Normalizes whitespace/case where applicable and validates tax/account formats; yields and sleeps 0.4 s. |
| 4. Cross-reference | `bank_country_match`, `name_match_check` (`pipeline.py:31-31`; `rules.py:245-262`; `rules.py:359-432`) | Same input -> results for bank-country consistency and company/account-holder name comparison. | Rejects a country mismatch; name comparison can use LLM or local matching. Yields and sleeps 0.4 s. |
| 5. Risk screening | `duplicate_check`, `email_domain_sanity` (`pipeline.py:32-32`; `rules.py:296-358`) | Same input + master list -> results for duplicate identity/account and flagged free email domain. | A different-name master match rejects; an empty master or free email causes pending review. Yields and sleeps 0.4 s. |
| 6. Decision | `submission_status`, `_decision_summary` (`pipeline.py:50-64`, `pipeline.py:96-131`; `rules.py:460-466`) | All rule results -> `final={"status":str,"reasons":list[RuleResult],"email_draft":None,"decision_summary":str}` plus `run_id` after saving. | Selects REJECTED before PENDING before APPROVED. `reasons` contains only non-`ok` results; the saved rule list contains every rule result except the synthetic `intake` result. Sleeps 0.4 s. Approved results are saved and yielded here, without Vendor communication. |
| 7. Vendor communication | `draft_vendor_email` and pipeline branch (`pipeline.py:131-162`; `llm.py:170-194`) | For non-approved results: vendor/contact/status and a list of failing rule messages -> final with `email_draft:str`, `run_id:int`; final is attached to the communication event. | Drafts an email, saves the run, then yields the completed stage. Sleeps 0.4 s. This stage is not emitted for APPROVED runs. |

### Individual rules

All eight rules receive `(submission: dict[str, Any], master_vendors: list[dict[str, Any]])` and return `{"rule": str, "passed": bool, "severity": "ok" | "pending" | "reject", "message": str}`. (`rules.py:12-17`; `rules.py:119-432`)

| Rule (implementation lines) | Check and severity |
|---|---|
| `completeness` (`rules.py:119-176`) | Required fields absent/blank, wrong basic types, text over 10,000 chars, empty documents, or unknown keys -> **pending**; otherwise **ok**. |
| `required_documents` (`rules.py:177-209`) | Required documents by country (IN/GB/DE: incorporation, tax registration, bank proof; US: W-9/W-8 and bank proof); unknown country or missing docs -> **pending**. |
| `tax_id_format` (`rules.py:210-244`) | Country-specific tax-ID regex; unsupported country or wrong format -> **reject**, with a different matching country format mentioned if found. |
| `bank_country_match` (`rules.py:245-262`) | Bank country must equal vendor country after trim/uppercase; mismatch -> **reject**. |
| `bank_account_format` (`rules.py:263-295`) | IN/US digits-only; GB/DE country-specific IBAN pattern; invalid/unsupported format -> **reject**. |
| `duplicate_check` (`rules.py:296-340`) | Normalized tax ID or account number matches a differently named master vendor -> **reject** (“Possible duplicate/fraud”); same normalized name is skipped; empty master -> **pending**. |
| `email_domain_sanity` (`rules.py:341-358`) | `gmail.com`, Yahoo, or Outlook listed domains -> **pending** (corporate email requested); other domains -> **ok**. |
| `name_match_check` (`rules.py:359-432`) | Missing/overlong names or an unconfirmed match -> **pending**; confidence under 0.6 plus visibly different names -> **reject**; an LLM match with lexical similarity under 0.85 -> **pending**; accepted match -> **ok**. |

`evaluate_submission` runs the eight rules in the tuple order; `submission_status` returns REJECTED if any rule rejects, else PENDING if any rule is pending, else APPROVED. (`rules.py:433-466`)

### Name matching, LLM call, and fallback

`fuzzy_name_match(company_name, bank_holder)` returns `{"match": bool, "confidence": float, "explanation": str, "source": "exact" | "llm" | "fallback"}`. Equal normalized names return `source:"exact"` without calling the LLM. Otherwise the prompt asks for JSON and treats names as untrusted text; valid JSON with correctly typed fields is accepted as an LLM result. (`llm.py:98-147`)

`call_llm(prompt)` reads settings through `config.get_setting`, which checks environment variables first and then Streamlit secrets. It supports Gemini, OpenAI, and Anthropic SDK calls with a configured 15-second client timeout; missing key, unsupported provider, or any caught exception returns `None`. The name fallback uses `SequenceMatcher` ratio >= 0.85; parse/response failures also fall back. (`config.py:7-21`; `llm.py:9-79`; `llm.py:80-97`; `llm.py:98-147`)

`draft_vendor_email(vendor_name, contact_name, status, issues)` asks the LLM for a short professional email containing only the supplied issue bullets. If the call fails or returns no usable text, `_fallback_vendor_email` creates a plain template and includes each non-empty issue. (`llm.py:148-194`)

### Database persistence

`db.save_run` receives `(vendor_name: str, status: str, reasons: list[RuleResult], email_draft: str | None, submission: dict[str, Any], parent_run_id: int | None)` and returns the inserted row ID as an integer. The SQLite row is shaped as:

```python
{
    "id": int,
    "timestamp": str,             # UTC ISO timestamp
    "vendor_name": str,
    "status": str,
    "reasons_json": str,          # JSON array of rule results
    "email_draft": str | None,
    "submission_json": str,       # Fernet-prefixed ciphertext
    "parent_run_id": int | None,
}
```

`db.init_db()` creates the DB parent directory and `runs` table if needed, and adds `parent_run_id` to an older table. Submission JSON is Fernet-encrypted; reasons and email fields are stored as ordinary text. (`db.py:12-55`; `db.py:56-84`; `db.py:113-121`)

`db.list_runs()` selects newest IDs first, decrypts submission JSON, and migrates legacy plaintext submissions by encrypting them on read. `db.clear_runs()` deletes all runs. The dashboard calls `list_runs`; the pipeline calls `init_db` and saves once for every completed run. (`db.py:85-124`; `pipeline.py:73-74`; `pipeline.py:114-162`; `app.py:251-252`; `app.py:371-373`; `app.py:522-528`)

### Live-run rendering and final display

`app._execute_pipeline` increments `run_generation`, clears prior run results, saves the current submission in session state, appends each event to `run_events`, and renders running/completed statuses. Completed stages show their rule messages and a severity-based icon. (`app.py:110-151`; `app.py:219-247`)

`app._render_final` displays the status badge and decision summary, builds a table from completed rule-stage results (excluding intake and decision/communication events), and displays an editable email plus copy-friendly code block for non-approved outcomes. It offers resubmission only for PENDING results with a saved run ID. Session state for events/final/submission/generation is initialized before the tabs render, so prior run details remain visible on rerun. (`app.py:153-217`; `app.py:269-280`; `app.py:338-369`)

### Dashboard

The Dashboard reads `db.list_runs()` and computes total/status counts and auto-approval percentage (zero when there are no runs); it renders the status chart. The history table filters by status and searches vendor name or actionable-rule summary. A run selector shows its reasons, email, decrypted submission, and parent-run label; the empty state says completed runs will appear here. (`app.py:370-405`; `app.py:430-529`)

“Run all samples” reads each sample and consumes `run_pipeline`, which persists each completed run; it reports file/JSON/value errors, then reruns the app. “Clear history” calls `db.clear_runs()`, clears run-related session state, and reruns. (`app.py:407-428`; `app.py:522-528`)

### Resubmission

The Run tab only offers “Simulate vendor resubmission” for PENDING outcomes. It passes the saved submission and failed reasons to `simulate_vendor_resubmission`, which returns `(corrected_submission: dict, changes: list[str])`; when changes exist, the UI calls the pipeline with the previous `run_id` as `parent_run_id`. The new DB row therefore links to the original. (`app.py:209-217`; `app.py:352-369`; `resubmission.py:7-99`; `pipeline.py:66-68`; `db.py:56-84`)

The simulator deep-copies through JSON, fills missing required fields with demo values, removes unknown fields, adds required documents, and applies demo corrections for a flagged email or name mismatch. It does not alter the original submission object. (`resubmission.py:7-99`)

## Data journey: `duplicate_bank.json`

The sample is a complete Indian submission for Blue Peak Logistics Pvt Ltd, with valid-looking GSTIN/account data and bank holder “Blue Peak Logistics Private Limited.” The account number `918273645001` is also in the master under Northstar Office Supplies, a differently named company. (`data/samples/duplicate_bank.json:1-18`; `data/vendors_master.json:1-6`)

| Layer | Data object after this step |
|---|---|
| Sample loader | The 12-key sample dict from `duplicate_bank.json` is read unchanged. Its document list contains `incorporation_cert`, `tax_registration`, and `bank_proof`. (`app.py:71-77`; `data/samples/duplicate_bank.json:1-18`) |
| Intake | The same dict enters `run_pipeline`; its done result is `{"rule":"intake","passed":true,"severity":"ok","message":"Submission JSON object received."}`. (`pipeline.py:66-86`) |
| Completeness & documents | Both results are `passed:true, severity:"ok"`; no submission fields are changed. (`pipeline.py:29-30`; `rules.py:119-209`) |
| Format validation | GSTIN and account-format results are both `passed:true, severity:"ok"`; the checked values remain `29AAFCB7890D1Z3` and `918273645001`. (`pipeline.py:30-30`; `rules.py:210-295`; `data/samples/duplicate_bank.json:4-10`) |
| Cross-reference | Bank country passes. Normalization maps the company and account holder to the same legal-suffix-normalized name, so name match passes with `source:"exact"`, confidence `1.0`; there is no LLM name call. (`pipeline.py:31-31`; `rules.py:359-432`; `llm.py:98-109`; `data/samples/duplicate_bank.json:2-11`) |
| Risk screening | `duplicate_check` returns `passed:false, severity:"reject"` because the account matches Northstar; email domain returns `passed:true, severity:"ok"`. (`pipeline.py:32-32`; `rules.py:296-358`; `data/vendors_master.json:1-6`) |
| Decision | `status:"REJECTED"`; `reasons` contains the duplicate finding; summary is `Rejected: Possible duplicate/fraud: bank account number matches a different approved vendor (Northstar Office Supplies Pvt Ltd).` (`pipeline.py:50-64`; `pipeline.py:96-131`; `rules.py:460-466`) |
| Communication and DB | With no usable LLM response, the fallback email includes that duplicate issue. The DB row stores status REJECTED, all eight rule results, the email, encrypted submission JSON, and `parent_run_id:null`; final also has `run_id`. (`pipeline.py:133-162`; `llm.py:148-194`; `db.py:56-84`; `db.py:113-121`) |
| Dashboard | The run appears in the status counts/history. Selecting it shows its rule results, email, and decrypted original submission. (`app.py:370-405`; `app.py:430-519`; `db.py:85-112`) |

## Where the LLM is used vs deterministic

| Step | LLM or rule | Fallback behavior |
|---|---|---|
| Completeness, documents, tax/bank formats, duplicate, email domain | Deterministic Python rules. (`pipeline.py:27-33`; `rules.py:119-358`) | No LLM is called; these checks produce their declared `ok`, `pending`, or `reject` findings. |
| Company/bank-holder name comparison | Exact normalized equality first; otherwise LLM may judge the relationship. (`llm.py:98-147`; `rules.py:359-432`) | Missing key/call failure/invalid response uses normalized-name similarity; >=85% means fallback match. |
| Vendor email | LLM drafts from the supplied status/issues. (`pipeline.py:133-142`; `llm.py:170-194`) | Rule-based template includes the supplied non-empty issues. |
| Final status | Deterministic severity precedence, independent of email wording. (`rules.py:460-466`; `pipeline.py:99-112`) | No LLM fallback needed. |

## Failure handling

- **Malformed upload:** invalid UTF-8/JSON and invalid top-level or country/documents shapes raise `SubmissionJSONError`; Run shows a friendly message and does not run the pipeline. A missing required field is not a parse error; `completeness` reports it as pending, while other rules still run. (`submission.py:9-26`; `app.py:311-323`; `rules.py:119-176`)
- **LLM unavailable:** `call_llm` returns `None` for missing key, unsupported provider, or a caught SDK exception; name matching and email drafting then use their local fallbacks. Name-match response parse/shape errors also fall back. (`llm.py:21-79`; `llm.py:98-147`; `llm.py:148-194`)
- **Master missing:** `_load_master_vendors` returns `[]`; duplicate screening reports pending manual review instead of silently passing. A malformed master JSON or non-list master raises an error. (`pipeline.py:165-173`; `rules.py:296-309`)
- **Database unavailable:** `init_db`, `save_run`, and dashboard `list_runs` do not catch SQLite or encryption errors; a DB failure can therefore interrupt pipeline execution or dashboard rendering. The app’s sample loader catches only file/JSON/value errors, not DB errors. (`db.py:29-55`; `db.py:56-112`; `pipeline.py:73-74`; `pipeline.py:114-149`; `app.py:407-428`; `app.py:371-373`)

## File map

| File | Responsibility | Key code / source lines |
|---|---|---|
| `app.py` | Authentication, input UI, stage display, final result, dashboard, assumptions. | `_require_login` (`app.py:40-69`); loaders (`app.py:71-108`); `_execute_pipeline` (`app.py:219-247`); Run UI (`app.py:282-369`); Dashboard (`app.py:370-529`); assumptions (`app.py:530-562`). |
| `submission.py` | Upload bytes to object parsing and validation. | `SubmissionJSONError`, `parse_submission_json` (`submission.py:5-26`). |
| `pipeline.py` | Ordered stages, generator events, decision, communication, and persistence calls. | `_STAGES`/`_event` (`pipeline.py:27-48`); `_decision_summary` (`pipeline.py:50-64`); `run_pipeline` (`pipeline.py:66-163`); master loader (`pipeline.py:165-173`). |
| `rules.py` | Pure checks, normalization, aggregation, and status precedence. | `normalize_company_name` (`rules.py:97-117`); eight rules (`rules.py:119-432`); `evaluate_submission`/`submission_status` (`rules.py:433-466`). |
| `llm.py` | Provider SDK call, fuzzy comparison, email draft, and local fallbacks. | `call_llm` (`llm.py:21-79`); name matching (`llm.py:80-147`); email drafting (`llm.py:148-194`). |
| `config.py` | `.env`, environment, and Streamlit-secrets setting lookup. | `load_dotenv` (`config.py:4-7`); `get_setting` (`config.py:10-21`). |
| `db.py` | SQLite schema, encrypted submission persistence, reads, and clearing. | `init_db` (`db.py:29-55`); `save_run` (`db.py:56-84`); `list_runs` (`db.py:85-112`); `clear_runs` (`db.py:122-124`). |
| `resubmission.py` | Builds a corrected demo submission and change descriptions. | `simulate_vendor_resubmission` (`resubmission.py:7-99`). |
| `data/submission.schema.json` | Declarative submission schema; the upload parser currently performs only its explicit structural checks and does not load this file. | Schema declarations (`data/submission.schema.json:1-20`); parser implementation (`submission.py:9-26`). |
| `data/samples/duplicate_bank.json` | Duplicate-account sample input. | Submission object (`data/samples/duplicate_bank.json:1-18`). |
| `data/vendors_master.json` | Approved-vendor comparison records. | Five vendor objects (`data/vendors_master.json:1-27`). |
