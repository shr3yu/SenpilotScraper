# Senpilot Technical Assessment 

An email-triggered agent that automatically retrieves regulatory documents from the [Nova Scotia Utility and Review Board (UARB)](https://uarb.novascotia.ca/fmi/webd/UARB15) public documents database, and replies with a structured summary and a ZIP of the requested files.

> 📄 [Thought process & design notes](https://docs.google.com/document/d/1PwkTMGCXHIEdLAXugiKMPIk2S7s2_joVP_iIDkhjRNA/edit?usp=sharing)

---

## How It Works

1. A user emails a matter number and document type to the agent's Gmail inbox
2. The agent parses the email using an LLM to extract the matter number and document type
3. A Playwright-based scraper navigates the UARB site, collects metadata, and downloads up to 10 documents
4. The documents are zipped and the agent replies with a summary email and the ZIP as an attachment

---

## Pipeline

```
Email In
  → LLM (parse matter number + doc type)
  → Playwright scraper (metadata + documents)
  → Zipper
  → LLM (compose reply)
  → Email Out (with ZIP attached)
```

---

## Project Structure

```
SenpilotScraper/
├── scraper.py        # Playwright scraper - navigates UARB, downloads PDFs
├── zipper.py         # Zips a directory of downloaded files
├── process.py        # Orchestrator - ties scraper + zipper + LLM together
├── email_agent.py    # Gmail poller - receives emails, runs pipeline, sends reply
├── requirements.txt  # Python dependencies
└── README.md
```

---

## Setup

### 1. Install dependencies
```
pip install -r requirements.txt
playwright install chromium
```

### 2. Set your Gemini API key
```
export GEMINI_API_KEY=your_key_here
```

### 3. Configure Gmail API
- Go to [Google Cloud Console](https://console.cloud.google.com)
- Create a project, enable the Gmail API
- Create an OAuth 2.0 Desktop App credential
- Download `credentials.json` and place it in the project root
- Add your Gmail address as a test user under OAuth consent screen → Audience → Test users

### 4. Run the agent
```
python3 email_agent.py
```
On first run, a browser will open for Gmail OAuth consent. After authenticating, `token.json` is created automatically and auth persists across runs.

---

## Sending a Request

Email the agent's Gmail address with a body like:

```
Hi, could you please send me the Exhibits for matter M12205?
```

**Valid document types:**
- Exhibits
- Key Documents
- Other Documents
- Transcripts
- Recordings

**Matter number format:** `M` followed by exactly 5 digits (e.g. `M12205`)

---

## Reply Format

```
Hi User,
M12205 is about Halifax Regional Water Commission - Windsor Street Exchange 
Redevelopment Project - $69,275,000. It relates to Capital Expenditure Approvals 
within the Water category. The matter had an initial filing on April 7, 2025 and 
a final filing on October 23, 2025. I found 13 Exhibits, 6 Key Documents, 43 Other 
Documents, and no Transcripts or Recordings. I downloaded 10 out of the 13 Exhibits 
and am attaching them as a ZIP here.
```

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Missing matter number or doc type | Replies asking for the missing information |
| Matter number not found | Replies with not found message + support email |
| Doc type has 0 documents | Replies noting no documents of that type exist |
| LLM API unavailable (503) | Retries up to 3 times before sending error reply |
| Partial scrape failure | Sends whatever was retrieved + flags the issue |

---

## Tech Stack

- **Playwright** — browser automation for FileMaker WebDirect scraping
- **Gemini API** — LLM for email parsing and reply composition
- **Gmail API** — inbox polling and email sending
- **Python stdlib** — zipfile, pathlib, email/MIME construction
