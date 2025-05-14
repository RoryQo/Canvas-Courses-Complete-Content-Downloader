import os
import requests
import pdfkit
import html2text
from bs4 import BeautifulSoup
from tqdm import tqdm
from pathlib import Path

def download_specific_courses(course_ids, token, output_dir):
    """
    Download files and linked module pages from specific Canvas courses (Mac users).
    """
    base_url = "https://canvas.instructure.com/api/v1"
    headers = {"Authorization": f"Bearer {token}"}
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    for course_id in course_ids:
        print(f"Processing course {course_id}...")
        download_course_files(course_id, headers, output_dir)
        download_course_modules(course_id, headers, output_dir)

def download_all_courses(token, output_dir):
    """
    Download files and linked module pages from all Canvas courses (Mac users).
    """
    base_url = "https://canvas.instructure.com/api/v1/courses?enrollment_state=active&per_page=100"
    headers = {"Authorization": f"Bearer {token}"}
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    response = requests.get(base_url, headers=headers)
    response.raise_for_status()
    courses = response.json()

    course_ids = [course['id'] for course in courses]
    print(f"Found {len(course_ids)} courses.")

    download_specific_courses(course_ids, token, output_dir)

def download_course_files(course_id, headers, output_dir):
    """Helper: Download all uploaded files in a course."""
    files_url = f"https://canvas.instructure.com/api/v1/courses/{course_id}/files?per_page=100"
    save_folder = Path(output_dir) / str(course_id) / "Files"
    save_folder.mkdir(parents=True, exist_ok=True)

    while files_url:
        resp = requests.get(files_url, headers=headers)
        resp.raise_for_status()
        files = resp.json()

        for file_info in tqdm(files, desc=f"Downloading course {course_id} files"):
            file_name = file_info['filename']
            file_url = file_info['url']
            file_path = save_folder / file_name

            file_resp = requests.get(file_url, headers=headers)
            file_resp.raise_for_status()
            with open(file_path, 'wb') as f:
                f.write(file_resp.content)

        files_url = None
        if 'next' in resp.links:
            files_url = resp.links['next']['url']

def download_course_modules(course_id, headers, output_dir):
    """Helper: Download module item linked files in a course."""
    modules_url = f"https://canvas.instructure.com/api/v1/courses/{course_id}/modules?per_page=100"
    response = requests.get(modules_url, headers=headers)
    response.raise_for_status()
    modules = response.json()

    save_folder = Path(output_dir) / str(course_id) / "Modules"
    save_folder.mkdir(parents=True, exist_ok=True)

    for module in modules:
        module_id = module['id']
        items_url = f"https://canvas.instructure.com/api/v1/courses/{course_id}/modules/{module_id}/items?per_page=100"
        items_resp = requests.get(items_url, headers=headers)
        items_resp.raise_for_status()
        items = items_resp.json()

        for item in items:
            if item['type'] == "Page":
                page_url = f"https://canvas.instructure.com/api/v1/courses/{course_id}/pages/{item['page_url']}"
                page_resp = requests.get(page_url, headers=headers)
                page_resp.raise_for_status()
                page_data = page_resp.json()
                html_content = page_data['body']

                soup = BeautifulSoup(html_content, "html.parser")
                links = soup.find_all('a', href=True)

                for link in links:
                    file_link = link['href']
                    download_linked_file(file_link, headers, save_folder)

def download_linked_file(url, headers, save_folder):
    """Helper: Download linked files inside a module page."""
    response = requests.get(url, headers=headers, stream=True)
    response.raise_for_status()

    parsed_url = requests.utils.urlparse(url)
    filename = os.path.basename(parsed_url.path)
    file_path = save_folder / filename

    with open(file_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
