import os
import json
import logging
import traceback
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "fasttrack_state.json")
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")

load_dotenv(ENV_FILE)
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DOMAIN = "https://www.fasttrack.govt.nz"

# Add or remove project URLs here as needed
PROJECTS = [
    "https://www.fasttrack.govt.nz/projects/alternative-to-the-brynderwyn-hills",
    "https://www.fasttrack.govt.nz/projects/university-of-auckland-student-centre-and-library",
    "https://www.fasttrack.govt.nz/projects/state-highway-1-whangarei-to-port-marsden-highway"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/"
}

def send_discord_notification(content, is_error=False):
    """Sends a formatted message to Discord."""
    if not WEBHOOK_URL:
        logging.error("DISCORD_WEBHOOK_URL not found in .env.")
        return
    
    # Prefix errors with an alert icon
    if is_error:
        content = f"⚠️ **TRACKER ERROR** ⚠️\n```\n{content}\n```"
        
    try:
        requests.post(WEBHOOK_URL, json={"content": content}, timeout=10).raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Failed to send Discord notification: {e}")

def fetch_soup(url):
    """Fetches and parses a URL using browser headers to bypass 403 blocks."""
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')

def extract_links(soup, base_url):
    """Extracts hyperlink URLs and text."""
    links = {}
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text(strip=True) or "[No Text]"
        full_url = urljoin(base_url, href).split('#')[0] 
        links[full_url] = text
    return links

def get_project_slug(url):
    """Extracts the project name from the URL for state tracking."""
    return url.strip('/').split('/')[-1]

def process_project(project_url, previous_project_state):
    """Scrapes a single project and returns its new state and any update messages."""
    project_slug = get_project_slug(project_url)
    
    main_soup = fetch_soup(project_url)
    if not main_soup:
        return previous_project_state, []

    # 1. Discover subpages specific to this project
    pages_to_check = {project_url}
    for a in main_soup.find_all('a', href=True):
        href = a['href']
        if href.startswith(f'/projects/{project_slug}'):
            full_url = urljoin(DOMAIN, href).split('#')[0]
            pages_to_check.add(full_url)
            
    # 2. Scrape current state
    current_state = {}
    for page in pages_to_check:
        soup = fetch_soup(page)
        if soup:
            current_state[page] = extract_links(soup, page)
            
    # 3. Detect changes
    messages = []
    
    new_pages = set(current_state.keys()) - set(previous_project_state.keys())
    if new_pages:
        messages.append("🚨 **NEW SUBPAGES DETECTED:**")
        for p in new_pages:
            messages.append(f"• <{p}>")
        messages.append("") 

    for page, current_links in current_state.items():
        if page in previous_project_state:
            previous_links = previous_project_state[page]
            new_links = set(current_links.keys()) - set(previous_links.keys())
            
            if new_links:
                messages.append(f"📄 **NEW CONTENT ON:** <{page}>")
                for link in new_links:
                    messages.append(f"• [{current_links[link]}](<{link}>)")
                messages.append("")

    return current_state, messages

def main():
    logging.info("Starting multi-project tracker...")
    
    # Load global state
    previous_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                previous_state = json.load(f)
        except json.JSONDecodeError:
            send_discord_notification("Failed to parse state JSON file. Starting fresh.", is_error=True)
            
    current_state = {}
    all_update_messages = []
    
    # Process each project independently
    for project_url in PROJECTS:
        project_slug = get_project_slug(project_url)
        logging.info(f"Checking {project_slug}...")
        
        try:
            prev_proj_state = previous_state.get(project_slug, {})
            new_proj_state, proj_messages = process_project(project_url, prev_proj_state)
            
            current_state[project_slug] = new_proj_state
            
            if proj_messages:
                # Format a section header for the specific project
                formatted_title = project_slug.replace('-', ' ').title()
                project_block = [f"# 🚧 {formatted_title} Updates", f"**Main Page:** <{project_url}>\n"] + proj_messages
                all_update_messages.extend(project_block)
                
        except Exception as e:
            # Catch errors for this specific project and notify Discord
            error_details = traceback.format_exc()
            error_msg = f"Failed checking {project_slug}:\n{error_details}"
            logging.error(error_msg)
            send_discord_notification(error_msg, is_error=True)
            
            # Retain the previous state for this project so we don't lose tracking data
            current_state[project_slug] = previous_state.get(project_slug, {})

    # Dispatch successful updates
    if all_update_messages:
        final_message = "\n".join(all_update_messages)
        print(final_message)
        send_discord_notification(final_message)
    else:
        logging.info("No updates found across tracked projects.")

    # Save state
    with open(STATE_FILE, 'w') as f:
        json.dump(current_state, f, indent=4)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Catch fatal script errors (e.g., file permission issues)
        fatal_error = traceback.format_exc()
        send_discord_notification(f"Fatal script error:\n{fatal_error}", is_error=True)
