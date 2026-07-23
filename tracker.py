import hashlib
import difflib
import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from bs4.element import Comment, Doctype, NavigableString, ProcessingInstruction
from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "fasttrack_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "fasttrack_tracker.log")
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

load_dotenv(ENV_FILE)
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DOMAIN = "https://www.fasttrack.govt.nz"
DOMAIN_HOST = urlparse(DOMAIN).netloc.lower()
SCHEMA_VERSION = 3
DISCORD_LIMIT = 1900
MAX_TEXT_DIFF_LINES = 24
MAX_TEXT_DIFF_CHARS = 1200
MAX_DIFF_LINE_CHARS = 220

# Add or remove project URLs here as needed.
PROJECTS = [
    "https://www.fasttrack.govt.nz/projects/alternative-to-the-brynderwyn-hills",
    "https://www.fasttrack.govt.nz/projects/university-of-auckland-student-centre-and-library",
    "https://www.fasttrack.govt.nz/projects/state-highway-1-whangarei-to-port-marsden-highway",
    "https://www.fasttrack.govt.nz/projects/state-highway-1-wellington-improvements",
    "https://www.fasttrack.govt.nz/projects/north-west-rapid-transit",
    "https://www.fasttrack.govt.nz/projects/state-highway-1-north-canterbury-woodend-bypass-project-belfast-to-pegasus",
    "https://www.fasttrack.govt.nz/projects/eden-park-2.1",
]

FILE_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
    ".zip",
}


def normalise_url(url, base_url=DOMAIN):
    """Return a stable absolute URL without fragments or cosmetic trailing slashes."""
    absolute = urljoin(base_url, url)
    parsed = urlparse(absolute)
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = re.sub(r"/{2,}", "/", parsed.path)
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def get_project_slug(url):
    return urlparse(normalise_url(url)).path.rstrip("/").split("/")[-1]


def is_fasttrack_url(url):
    return urlparse(url).netloc.lower() == DOMAIN_HOST


def is_file_url(url):
    path = urlparse(url).path.lower()
    _, ext = os.path.splitext(path)
    return ext in FILE_EXTENSIONS


def is_project_subpage(url, project_url):
    parsed = urlparse(url)
    project_path = urlparse(normalise_url(project_url)).path.rstrip("/")
    path = parsed.path.rstrip("/")
    return (
        parsed.netloc.lower() == DOMAIN_HOST
        and path.startswith(f"{project_path}/")
        and not is_file_url(url)
    )


def send_discord_notification(content, is_error=False):
    if not WEBHOOK_URL:
        logging.info("DISCORD_WEBHOOK_URL is not set; skipping Discord notification.")
        return

    if is_error:
        content = f"**TRACKER ERROR**\n```text\n{content[:1800]}\n```"

    for chunk in split_discord_message(content):
        try:
            requests.post(WEBHOOK_URL, json={"content": chunk}, timeout=10).raise_for_status()
        except RequestException as exc:
            logging.error("Failed to send Discord notification: %s", exc)


def split_discord_message(content):
    lines = content.splitlines()
    chunks = []
    current = []
    in_code_block = False
    code_block_language = ""

    def chunk_length(chunk_lines):
        return len("\n".join(chunk_lines))

    def code_fence_language(line):
        stripped = line.strip()
        if not stripped.startswith("```"):
            return None
        return stripped[3:].strip()

    def close_current_chunk():
        nonlocal current
        if in_code_block:
            current.append("```")
        chunks.append("\n".join(current))
        current = []
        if in_code_block:
            opener = f"```{code_block_language}".rstrip()
            current.append(opener)

    for line in lines:
        fence_language = code_fence_language(line)
        will_be_in_code_block = in_code_block
        if fence_language is not None:
            will_be_in_code_block = not in_code_block

        candidate = [*current, line]
        if will_be_in_code_block:
            candidate = [*candidate, "```"]

        if current and chunk_length(candidate) > DISCORD_LIMIT:
            close_current_chunk()

        current.append(line)

        if fence_language is not None:
            if in_code_block:
                in_code_block = False
                code_block_language = ""
            else:
                in_code_block = True
                code_block_language = fence_language

    if current:
        if in_code_block:
            current.append("```")
        chunks.append("\n".join(current))

    return chunks or [content[:DISCORD_LIMIT]]


def fetch_soup(url):
    response = requests.get(url, impersonate="chrome", timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def content_root(soup):
    root = soup.find("main") or soup.body or soup
    for tag in root(["script", "style", "noscript", "svg", "header", "footer", "nav", "form"]):
        tag.decompose()
    remove_site_alerts(root)
    return root


def remove_site_alerts(root):
    for tag in root.find_all(True):
        text = normalise_text(tag.get_text(" ", strip=True))
        if text.startswith("Info Our office is closed") and len(text) < 300:
            tag.decompose()


def normalise_text(text):
    return re.sub(r"\s+", " ", text).strip()


BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "details",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "li", "main", "ol", "p", "pre", "section", "summary",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


def extract_block_text(root):
    """Return page text with one line per block-level element (li, p, heading, ...)."""
    segments = []
    buffer = []

    def flush():
        text = normalise_text(" ".join(buffer))
        buffer.clear()
        if text:
            segments.append(text)

    def walk(node):
        for child in node.children:
            if isinstance(child, (Comment, Doctype, ProcessingInstruction)):
                continue
            if isinstance(child, NavigableString):
                buffer.append(str(child))
            elif child.name in BLOCK_TAGS:
                flush()
                walk(child)
                flush()
            else:
                walk(child)

    walk(root)
    flush()
    return "\n".join(segments)


def extract_page_text(soup):
    root = content_root(soup)
    title = normalise_text((root.find(["h1", "title"]) or soup.find("title") or root).get_text(" ", strip=True))
    return title, extract_block_text(root)


def extract_links(soup, page_url):
    links = {}
    for anchor in content_root(soup).find_all("a", href=True):
        url = normalise_url(anchor["href"], page_url)
        if not url.startswith(("http://", "https://")):
            continue

        text = normalise_text(anchor.get_text(" ", strip=True)) or "[No text]"
        if url not in links or len(text) > len(links[url]["text"]):
            links[url] = {
                "text": text,
                "kind": link_kind(url),
            }
    return dict(sorted(links.items()))


def link_kind(url):
    if is_file_url(url):
        return "file"
    if is_fasttrack_url(url):
        return "fasttrack-page"
    return "external"


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover_project_pages(project_url, main_soup):
    pages = {normalise_url(project_url)}
    project_url = normalise_url(project_url)

    for anchor in content_root(main_soup).find_all("a", href=True):
        url = normalise_url(anchor["href"], project_url)
        if is_project_subpage(url, project_url):
            pages.add(url)

    return sorted(pages)


def scrape_page(page_url):
    soup = fetch_soup(page_url)
    title, text = extract_page_text(soup)
    return {
        "title": title,
        "text_hash": fingerprint(text),
        "text": text,
        "links": extract_links(soup, page_url),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def load_previous_state():
    if not os.path.exists(STATE_FILE):
        return {"_schema_version": SCHEMA_VERSION, "projects": {}}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            data = json.load(state_file)
    except json.JSONDecodeError:
        send_discord_notification("Failed to parse state JSON file. Starting fresh.", is_error=True)
        return {"_schema_version": SCHEMA_VERSION, "projects": {}}

    if data.get("_schema_version") != SCHEMA_VERSION or "projects" not in data:
        raise ValueError(
            f"{STATE_FILE} is not a schema version {SCHEMA_VERSION} state file. "
            "Delete it and run again to create a fresh baseline."
        )

    return data


def page_changed(previous_page, current_page):
    previous_hash = previous_page.get("text_hash")
    return bool(previous_hash) and previous_hash != current_page["text_hash"]


def split_text_for_diff(text):
    return [line for line in text.split("\n") if line]


def truncate_diff_line(line):
    if len(line) <= MAX_DIFF_LINE_CHARS:
        return line
    return f"{line[: MAX_DIFF_LINE_CHARS - 3]}..."


def text_diff(previous_text, current_text):
    previous_lines = split_text_for_diff(previous_text)
    current_lines = split_text_for_diff(current_text)
    diff_lines = list(
        difflib.unified_diff(
            previous_lines,
            current_lines,
            fromfile="previous",
            tofile="current",
            lineterm="",
            n=2,
        )
    )
    interesting_lines = [
        truncate_diff_line(line)
        for line in diff_lines
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]

    if not interesting_lines:
        return ""

    truncated = len(interesting_lines) > MAX_TEXT_DIFF_LINES
    interesting_lines = interesting_lines[:MAX_TEXT_DIFF_LINES]
    diff = "\n".join(interesting_lines)
    if len(diff) > MAX_TEXT_DIFF_CHARS:
        diff = f"{diff[: MAX_TEXT_DIFF_CHARS - 20].rstrip()}\n... diff truncated"
    elif truncated:
        diff = f"{diff}\n... diff truncated"

    return f"```diff\n{diff}\n```"


def describe_link(link):
    label = link["text"]
    if link["kind"] == "file":
        label = f"{label} ({link['kind']})"
    return label


def compare_project(project_url, previous_project, current_project):
    previous_pages = previous_project.get("pages", {})
    current_pages = current_project["pages"]
    messages = []

    if not previous_pages:
        logging.info("No previous state for %s; saving baseline without alerting.", project_url)
        return messages

    previous_page_urls = set(previous_pages)
    current_page_urls = set(current_pages)

    new_pages = sorted(current_page_urls - previous_page_urls)
    if new_pages:
        messages.append("**New subpages**")
        messages.extend(f"- <{page_url}>" for page_url in new_pages)

    for page_url in sorted(current_page_urls):
        current_page = current_pages[page_url]
        previous_page = previous_pages.get(page_url)
        if not previous_page:
            continue

        page_messages = []
        if page_changed(previous_page, current_page):
            page_messages.append("- Page text changed:")
            diff = text_diff(previous_page.get("text", ""), current_page.get("text", ""))
            if diff:
                page_messages.append(diff)

        previous_links = previous_page.get("links", {})
        current_links = current_page.get("links", {})
        new_links = sorted(set(current_links) - set(previous_links))
        removed_links = sorted(set(previous_links) - set(current_links))

        if new_links:
            page_messages.append("- New files/links:")
            for link_url in new_links:
                page_messages.append(f"  - [{describe_link(current_links[link_url])}](<{link_url}>)")

        changed_link_text = [
            link_url
            for link_url in sorted(set(current_links) & set(previous_links))
            if current_links[link_url].get("text") != previous_links[link_url].get("text")
        ]
        if changed_link_text:
            page_messages.append("- Link text changed:")
            for link_url in changed_link_text:
                old_text = previous_links[link_url].get("text", "")
                new_text = current_links[link_url].get("text", "")
                page_messages.append(f"  - <{link_url}>: `{old_text}` -> `{new_text}`")

        if removed_links:
            page_messages.append("- Removed files/links:")
            for link_url in removed_links:
                page_messages.append(f"  - <{link_url}>")

        if page_messages:
            title = current_page.get("title") or page_url
            messages.append(f"**Changed page:** [{title}](<{page_url}>)")
            messages.extend(page_messages)

    return messages


def process_project(project_url, previous_project):
    normalised_project_url = normalise_url(project_url)
    logging.info("Fetching project landing page: %s", normalised_project_url)
    main_soup = fetch_soup(normalised_project_url)
    pages_to_check = discover_project_pages(normalised_project_url, main_soup)

    logging.info("Discovered %s project page(s).", len(pages_to_check))
    current_project = {
        "url": normalised_project_url,
        "pages": {},
    }

    for page_url in pages_to_check:
        logging.info("Scraping %s", page_url)
        current_project["pages"][page_url] = scrape_page(page_url)

    messages = compare_project(normalised_project_url, previous_project, current_project)
    return current_project, messages


def save_state(state):
    tmp_file = f"{STATE_FILE}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, indent=2, ensure_ascii=False)
        state_file.write("\n")
    os.replace(tmp_file, STATE_FILE)


def main():
    logging.info("Starting Fast-track tracker.")
    previous_state = load_previous_state()
    current_state = {
        "_schema_version": SCHEMA_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "projects": {},
    }
    all_update_messages = []

    for project_url in PROJECTS:
        project_slug = get_project_slug(project_url)
        logging.info("Checking %s", project_slug)

        try:
            previous_project = previous_state.get("projects", {}).get(project_slug, {})
            current_project, messages = process_project(project_url, previous_project)
            current_state["projects"][project_slug] = current_project

            if messages:
                title = project_slug.replace("-", " ").title()
                all_update_messages.extend(
                    [
                        f"# {title} updates",
                        f"Main page: <{normalise_url(project_url)}>",
                        *messages,
                        "",
                    ]
                )
        except Exception:
            error_details = traceback.format_exc()
            logging.error("Failed checking %s:\n%s", project_slug, error_details)
            send_discord_notification(f"Failed checking {project_slug}:\n{error_details}", is_error=True)
            current_state["projects"][project_slug] = previous_state.get("projects", {}).get(project_slug, {})

    if all_update_messages:
        final_message = "\n".join(all_update_messages).strip()
        print(final_message)
        send_discord_notification(final_message)
    else:
        logging.info("No updates found across tracked projects.")

    save_state(current_state)
    logging.info("State saved to %s", STATE_FILE)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        fatal_error = traceback.format_exc()
        logging.error("Fatal script error:\n%s", fatal_error)
        send_discord_notification(f"Fatal script error:\n{fatal_error}", is_error=True)
