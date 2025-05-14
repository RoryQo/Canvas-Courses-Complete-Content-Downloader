import os
import requests
import pdfkit
import html2text
from bs4 import BeautifulSoup
from tqdm import tqdm
from pathlib import Path

def download_specific_courses(course_ids, token, output_dir, base_url="https://canvas.instructure.com/api/v1"):
    """
    Download files and linked module pages from specific Canvas courses (Windows users).
    """
    headers = {"Authorization": f"Bearer {token}"}
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    config = pdfkit.configuration(wkhtmltopdf=r"C:/Program Files/wkhtmltopdf/bin/wkhtmltopdf.exe")

    for course_id in course_ids:
        print(f"Processing course {course_id}...")
        download_course_files(course_id, headers, output_dir, base_url)
        download_course_modules(course_id, headers, output_dir, config, base_url)

def download_all_courses(token, output_dir, base_url="https://canvas.instructure.com/api/v1"):
    """
    Download files and linked module pages from all Canvas courses (Windows users).
    """
    headers = {"Authorization": f"Bearer {token}"}
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    config = pdfkit.configuration(wkhtmltopdf=r"C:/Program Files/wkhtmltopdf/bin/wkhtmltopdf.exe")

    courses_url = f"{base_url}/courses?enrollment_state=active&per_page=100"
    response = requests.get(courses_url, headers=headers)
    response.raise_for_status()
    courses = response.json()

    course_ids = [course['id'] for course in courses]
    print(f"Found {len(course_ids)} courses.")

    download_specific_courses(course_ids, token, output_dir, base_url)

def download_course_files(course_id, headers, output_dir, base_url):
    """Helper: Download all uploaded files in a course."""
    files_url = f"{base_url}/courses/{course_id}/files?per_page=100"
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

def download_course_modules(course_id, headers, output_dir, config, base_url):
    """Helper: Download module item linked files in a course."""
    modules_url = f"{base_url}/courses/{course_id}/modules?per_page=100"
    response = requests.get(modules_url, headers=headers)
    response.raise_for_status()
    modules = response.json()

    save_folder = Path(output_dir) / str(course_id) / "Modules"
    save_folder.mkdir(parents=True, exist_ok=True)

    for module in modules:
        module_id = module['id']
        items_url = f"{base_url}/courses/{course_id}/modules/{module_id}/items?per_page=100"
        items_resp = requests.get(items_url, headers=headers)
        items_resp.raise_for_status()
        items = items_resp.json()

        for item in items:
            if item['type'] == "Page":
                page_url = f"{base_url}/courses/{course_id}/pages/{item['page_url']}"
                page_resp = requests.get(page_url, headers=headers)
                page_resp.raise_for_status()
                page_data = page_resp.json()

                pdf_file_path = save_folder / (item['title'].replace('/', '-') + ".pdf")
                pdfkit.from_url(page_data['html_url'], str(pdf_file_path), configuration=config)
