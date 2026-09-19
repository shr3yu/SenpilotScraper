"""
Orchestrator - ties scraper + zipper + LLM together.

Flow:
    email in → parse_email() → MatterRequest
                             → run_matter() → ScrapeResult
                             → zip_directory() → zip_path
                             → compose_reply() → reply email text
    email out (with zip attached)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from scraper import (
    DocType,
    MatterNotFoundError,
    MatterRequest,
    NoDocumentsError,
    ScrapeResult,
    format_date,
    run_matter,
)
from zipper import zip_directory

from google import genai
from google.genai import types

client = genai.Client()
MODEL = "gemini-3.5-flash-lite"
SUPPORT_EMAIL = "customer.support@gmail.com"


# ── Prompts ───────────────────────────────────────────────────────────────────

PARSE_SYSTEM = """
You extract a matter number and document type from an email.
Reply ONLY with valid JSON, no markdown formatting blocks, no explanation.

Schema:
{
  "matter_number": "M12345" | null,
  "doc_type": "Exhibits" | "Key Documents" | "Other Documents" | "Transcripts" | "Recordings" | null,
  "missing": ["matter_number"] | ["doc_type"] | ["matter_number", "doc_type"] | []
}

Rules:
- matter_number must match M followed by exactly 5 digits. If absent or malformed, set null and add to missing.
- doc_type must be one of the five exact strings above (case-insensitive match is fine, but output the canonical form).
  If absent or unrecognisable, set null and add to missing.
- missing lists only the fields that are absent/invalid.
"""

COMPOSE_SYSTEM = f"""
You write a short, professional reply email on behalf of a document retrieval service.
You are given structured data as JSON. Use it exactly — do not invent or alter any figures, dates, or names.
Do not include a subject line. Start directly with "Hi User,".

For successful responses, follow this exact sentence structure:
Hi User,
{{matter_number}} is about {{description}}. It relates to {{category}} within the {{type}} category. The matter had an initial filing on {{date_received}} and a final filing on {{decision_date}}. I found {{counts sentence}}. I downloaded {{downloaded}} out of the {{total_available}} {{doc_type_requested}} and am attaching them as a ZIP here.

Rules for the counts sentence:
- List all five doc types in this order: Exhibits, Key Documents, Other Documents, Transcripts, Recordings.
- Use the exact count from tab_counts for each.
- If a count is 0, say "no Transcripts" / "no Recordings" (drop the number).
- If both Transcripts and Recordings are 0, combine them: "no Transcripts or Recordings".
- Example: "I found 13 Exhibits, 6 Key Documents, 43 Other Documents, and no Transcripts or Recordings."

For error responses, use the error field:
- matter_not_found: tell the user no records were found for that matter number, and to contact {SUPPORT_EMAIL}.
- no_documents: tell the user no documents of that type were found, and to contact {SUPPORT_EMAIL}.
- incomplete_request: list missing fields and ask the user to reply with them.
- partial_success: note issues retrieving information and ask the user to contact {SUPPORT_EMAIL}.
"""

def compose_reply(data: dict) -> str:
    response = client.models.generate_content(
        model=MODEL,
        contents=json.dumps(data),
        config=types.GenerateContentConfig(
            system_instruction=COMPOSE_SYSTEM,
            max_output_tokens=2048,  # Increased to 2048 for safe headroom
        ),
    )
    text_content = "".join(
        part.text for c in response.candidates 
        for part in c.content.parts 
        if getattr(part, "text", None) and not hasattr(part, "thought_signature")
    ).strip() or (response.text or "").strip()
    
    return text_content

# ── LLM call 1: parse incoming email ─────────────────────────────────────────

@dataclass
class ParseResult:
    matter_number: str | None
    doc_type: DocType | None
    missing: list[str]


def parse_email(email_body: str) -> ParseResult:
    response = client.models.generate_content(
        model=MODEL,
        contents=email_body,
        config=types.GenerateContentConfig(
            system_instruction=PARSE_SYSTEM,
            response_mime_type="application/json",
            max_output_tokens=1024,
        ),
    )

    # Gather text from valid response parts
    text_content = "".join(
        part.text for c in response.candidates 
        for part in c.content.parts 
        if getattr(part, "text", None) and not hasattr(part, "thought_signature")
    ).strip() or (response.text or "").strip()

    # Isolate JSON object between first '{' and last '}'
    start, end = text_content.find("{"), text_content.rfind("}")
    if start != -1 and end != -1 and end > start:
        text_content = text_content[start:end + 1]

    raw = json.loads(text_content)
    return ParseResult(
        matter_number=raw.get("matter_number"),
        doc_type=DocType(raw["doc_type"]) if raw.get("doc_type") else None,
        missing=raw.get("missing", []),
    )


# ── LLM call 2: compose reply email ──────────────────────────────────────────

def compose_reply(data: dict) -> str:
    response = client.models.generate_content(
        model=MODEL,
        contents=json.dumps(data),
        config=types.GenerateContentConfig(
            system_instruction=COMPOSE_SYSTEM,
            max_output_tokens=2048,
        ),
    )
    # Extract only text parts.
    text_content = ""
    for candidate in response.candidates:
        for part in candidate.content.parts:
            if hasattr(part, "text") and part.text:
                text_content += part.text
    return text_content.strip()


def compose_not_found(matter_number: str) -> str:
    return compose_reply({"error": "matter_not_found", "matter_number": matter_number})


def compose_no_documents(matter_number: str, doc_type: str) -> str:
    return compose_reply({"error": "no_documents", "matter_number": matter_number, "doc_type": doc_type})


def compose_missing_fields(missing: list[str]) -> str:
    return compose_reply({"error": "incomplete_request", "missing_fields": missing})


def compose_partial(reply_data: dict, failed_steps: list[str]) -> str:
    return compose_reply({
        "error": "partial_success",
        "failed_steps": failed_steps,
        "partial_data": reply_data,
    })


# ── Orchestrator ──────────────────────────────────────────────────────────────

@dataclass
class PipelineResult:
    reply_text: str
    zip_path: Path | None  # None when there are no files to attach


def process_email(email_body: str) -> PipelineResult:
    # Step 1: parse
    parsed = parse_email(email_body)
    if parsed.missing:
        return PipelineResult(
            reply_text=compose_missing_fields(parsed.missing),
            zip_path=None,
        )

    request = MatterRequest(
        matter_number=parsed.matter_number,
        doc_type=parsed.doc_type,
    )

    # Step 2: scrape
    try:
        result: ScrapeResult = run_matter(request, headless=True)
    except MatterNotFoundError as e:
        return PipelineResult(reply_text=compose_not_found(str(e)), zip_path=None)
    except NoDocumentsError as e:
        return PipelineResult(reply_text=compose_no_documents(request.matter_number, str(e)), zip_path=None)

    # Step 3: zip with matterNumber_documentType_id naming format
    failed_steps = []
    zip_path = None
    try:
        # result.download_dir.name gives 'M12205_a1b2c3d4'
        matter_num, unique_id = result.download_dir.name.split("_")
        doc_label = request.doc_type.value.replace(" ", "_")
        
        # Build the final filename: M12205_Exhibits_a1b2c3d4.zip
        unique_zip_name = f"{matter_num}_{doc_label}_{unique_id}.zip"
        
        zip_path = zip_directory(
            result.download_dir,
            Path("outputs") / unique_zip_name,
        )
    except Exception as e:
        print(f"[zip] Failed: {e}")
        failed_steps.append("zip")

    # Step 4: compose reply
    m = result.metadata
    reply_data = {
        "matter_number": m.matter_number,
        "description": m.description,
        "status": m.status,
        "type": m.type_,
        "category": m.category,
        "date_received": format_date(m.date_received),
        "decision_date": format_date(m.decision_date),
        "doc_type_requested": request.doc_type.value,
        "tab_counts": {k.value: v for k, v in result.tab_counts.items()},
        "downloaded": result.downloaded_count,
        "total_available": result.tab_counts.get(request.doc_type, 0),
    }

    reply_text = compose_partial(reply_data, failed_steps) if failed_steps else compose_reply(reply_data)

    return PipelineResult(reply_text=reply_text, zip_path=zip_path)


if __name__ == "__main__":
    # Test with a fake email before wiring up real email sending.
    test_email = """
    Hi, could you send me the Exhibits for matter M12205?
    Thanks
    """
    result = process_email(test_email)
    print("Reply:\n", result.reply_text)
    print("Zip:", result.zip_path)