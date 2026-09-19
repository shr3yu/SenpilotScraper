from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

BASE_URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"
NOT_FOUND_MESSAGE = "No records matched your search request"


class MatterNotFoundError(Exception):
    """Raised when the site reports no matching records."""


class NoDocumentsError(Exception):
    """Raised when the requested doc type has 0 documents available."""


class DocType(str, Enum):
    EXHIBITS = "Exhibits"
    KEY_DOCUMENTS = "Key Documents"
    OTHER_DOCUMENTS = "Other Documents"
    TRANSCRIPTS = "Transcripts"
    RECORDINGS = "Recordings"


# Tab bar button classes, confirmed from live DOM.
TAB_SEGMENT_CLASSES: dict[DocType, str] = {
    DocType.EXHIBITS: "fm_object_277",
    DocType.KEY_DOCUMENTS: "fm_object_278",
    DocType.OTHER_DOCUMENTS: "fm_object_279",
    DocType.TRANSCRIPTS: "fm_object_280",
    DocType.RECORDINGS: "fm_object_281",
}


@dataclass
class MatterRequest:
    """Input to the scrape - what the parse-email LLM step will produce."""
    matter_number: str
    doc_type: DocType


@dataclass
class MatterMetadata:
    matter_number: str
    description: str
    category: str
    type_: str
    date_received: str
    decision_date: str
    status: str


@dataclass
class ScrapeResult:
    metadata: MatterMetadata
    tab_counts: dict[DocType, int]
    download_dir: Path
    downloaded_count: int


def launch_browser(headless: bool = True):
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    return playwright, browser, page


def search_matter(page: Page, matter_number: str) -> None:
    print(f"[1/4] Navigating to {BASE_URL}...")
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    # 1. Wait for the custom FileMaker widget to actually be visible in the DOM
    container = page.locator(".fm_object_254")
    container.wait_for(state="visible", timeout=15000)

    print(f"[1/4] Typing matter number: {matter_number}")
    
    # 2. Click the fake input div to trigger FileMaker's focus scripts
    page.locator(".fm_object_254 .text").click()

    # 3. CRITICAL: Wait a moment for Vaadin to swap the div for a real editable field
    page.wait_for_timeout(500) 

    # 4. Type the value
    page.keyboard.type(matter_number)

    # Two "Search" buttons on this page - use the one next to "Go Directly to Matter".
    print("[1/4] Clicking Search...")
    page.locator(".fm_object_258").click()
    page.locator(".fm_object_258").click()

    # Race: either the matter page loads or a "not found" dialog appears.
    print("[1/4] Waiting for results...")
    loaded = page.locator(".fm_object_286")
    not_found = page.get_by_text(NOT_FOUND_MESSAGE)
    loaded.or_(not_found).first.wait_for(state="visible", timeout=15000)

    if not_found.is_visible():
        page.get_by_role("button", name="OK").click()
        raise MatterNotFoundError(matter_number)
    print("[1/4] Matter page loaded.")

    page.wait_for_timeout(3000)


def scrape_metadata(page: Page, matter_number: str) -> MatterMetadata:
    print("[2/4] Scraping metadata...")
    
    # 1. Standard Playwright extraction for fields with stable fm_object classes
    def field_value(css_class: str) -> str:
        return page.locator(f".{css_class}").inner_text().strip()

    # 2. Beautiful Soup extraction specifically for the unlabelled Title/Description
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    
    # Target the unique inline styles from the HTML snippet you provided
    title_span = soup.find(
        "span", 
        style=re.compile(r"color:\s*#3C6F89;.*?font-weight:\s*bold", re.IGNORECASE)
    )
    description_text = title_span.get_text(strip=True) if title_span else "Unknown"

    metadata = MatterMetadata(
        matter_number=matter_number,
        description=description_text, 
        type_=field_value("fm_object_298"),
        category=field_value("fm_object_287"),
        date_received=field_value("fm_object_292"),
        decision_date=field_value("fm_object_294"),
        status=field_value("fm_object_289"),
    )
    print(f"[2/4] Metadata scraped: {metadata}")
    return metadata


def scrape_tab_counts(page: Page) -> dict[DocType, int]:
    """Parse counts from tab labels like 'Exhibits - 13'."""
    print("[3/4] Scraping tab counts...")
    counts: dict[DocType, int] = {}
    for doc_type, css_class in TAB_SEGMENT_CLASSES.items():
        tab_text = page.locator(f".{css_class}").inner_text()
        match = re.search(r"-\s*(\d+)", tab_text)
        counts[doc_type] = int(match.group(1)) if match else 0
    print(f"[3/4] Tab counts: {counts}")
    return counts


def format_date(raw: str) -> str:
    """'04/07/2025' -> 'April 7, 2025'."""
    try:
        dt = datetime.strptime(raw.strip(), "%m/%d/%Y")
        return dt.strftime("%B %-d, %Y")
    except ValueError:
        return raw


def download_documents(
    page: Page,
    doc_type: DocType,
    download_dir: Path,
    limit: int = 10,
) -> int:
    """Downloads up to `limit` PDFs, handling virtualized DOM recycling."""
    download_dir.mkdir(parents=True, exist_ok=True)

    print(f"[4/4] Clicking tab: {doc_type.value}")
    page.locator(f".{TAB_SEGMENT_CLASSES[doc_type]}").click()
    page.wait_for_timeout(2000)

    downloaded_count = 0
    processed_row_texts = set()
    scroll_attempts = 0

    while downloaded_count < limit and scroll_attempts < 5:
        # Give Vaadin time to render any newly scrolled rows
        page.wait_for_timeout(1000)
        
        button_count = page.get_by_text("GO GET IT").count()
        if button_count == 0:
            break

        found_new_in_this_pass = False

        for i in range(button_count):
            if downloaded_count >= limit:
                break
                
            button = page.get_by_text("GO GET IT").nth(i)
            
            # Identify the row by grabbing all text inside its parent container
            try:
                # Vaadin rows are usually wrapped in tr or div.v-grid-row
                row = button.locator("xpath=ancestor::*[contains(@class, 'v-grid-row') or local-name()='tr']").first
                row_text = row.inner_text()
            except Exception:
                row_text = f"fallback_{scroll_attempts}_{i}"
            
            # If we've already downloaded this exact row, skip to the next visible one
            if row_text in processed_row_texts:
                continue

            print(f"[4/4] Downloading {downloaded_count + 1}/{limit}...")
            button.click()

            print(f"[4/4]   Waiting for download dialog...")
            page.locator(".v-window-contents").wait_for(state="visible", timeout=15000)
            enabled_btn = page.locator(".fm-download-button:not(.v-disabled)")
            enabled_btn.wait_for(state="visible", timeout=15000)

            filename = page.locator(".fm-download-button .v-button-caption").inner_text().strip()
            if not filename.endswith(".pdf"):
                filename = f"{doc_type.value.replace(' ', '_')}_{downloaded_count + 1}.pdf"
            print(f"[4/4]   Filename: {filename}")

            with page.expect_download(timeout=30000) as download_info:
                enabled_btn.click()

            download = download_info.value
            download.save_as(download_dir / filename)
            
            downloaded_count += 1
            processed_row_texts.add(row_text)
            found_new_in_this_pass = True
            scroll_attempts = 0  # Reset scroll attempts since we made progress
            
            print(f"[4/4]   Saved to {download_dir / filename}")

            page.get_by_role("button", name="Close").click()
            page.locator(".v-window-contents").wait_for(state="detached", timeout=5000)
            page.wait_for_timeout(500)

        # If we checked every button currently in the DOM and they were all already processed,
        # it is time to scroll the grid down to load fresh ones.
        if not found_new_in_this_pass:
            print(f"[4/4]   Scrolling to reveal more items (attempt {scroll_attempts + 1})...")
            try:
                last_btn = page.get_by_text("GO GET IT").last
                last_btn.hover()
                last_btn.focus()
                page.keyboard.press("PageDown")
                page.mouse.wheel(0, 1000)
            except Exception:
                pass
                
            page.wait_for_timeout(2000)
            scroll_attempts += 1

    if scroll_attempts >= 5:
        print(f"[4/4]   Reached the bottom of the virtual list. Stopping.")

    return downloaded_count

def run_matter(request: MatterRequest, headless: bool = True) -> ScrapeResult:
    playwright, browser, page = launch_browser(headless=headless)
    try:
        search_matter(page, request.matter_number)
        metadata = scrape_metadata(page, request.matter_number)
        tab_counts = scrape_tab_counts(page)

        # Bail early if the requested doc type has 0 documents.
        available = tab_counts.get(request.doc_type, 0)
        if available == 0:
            raise NoDocumentsError(request.doc_type.value)

        request_id = uuid.uuid4().hex[:8]
        download_dir = Path("downloads") / f"{request.matter_number}_{request_id}"
        
        # Limit changed to 10 here to grab all 10 documents
        downloaded_count = download_documents(page, request.doc_type, download_dir, limit=10)

        return ScrapeResult(
            metadata=metadata,
            tab_counts=tab_counts,
            download_dir=download_dir,
            downloaded_count=downloaded_count,
        )
    finally:
        browser.close()
        playwright.stop()


if __name__ == "__main__":
    test_request = MatterRequest(matter_number="M12205", doc_type=DocType.EXHIBITS)

    try:
        result = run_matter(test_request, headless=False)
        print("Metadata:", result.metadata)
        print("Tab counts:", result.tab_counts)
        print("Download dir:", result.download_dir)
        print("Downloaded count:", result.downloaded_count)
    except MatterNotFoundError as e:
        print(f"Not found: {e}")
    except NoDocumentsError as e:
        print(f"No documents: {e}")