#!/usr/bin/env python3
"""Archive a Canvas course using the Canvas REST API.

This CLI intentionally avoids browser scraping. It uses Canvas endpoints under
/api/v1 as the source of truth and writes a local HTML navigation site.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm


DEFAULT_TOKEN_ENV = "CANVAS_API_TOKEN"
DEFAULT_OUTPUT_DIR = "canvas_all_content"
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
RESOURCE_ATTRS = {
    "a": "href",
    "iframe": "src",
    "img": "src",
    "video": "src",
    "audio": "src",
    "source": "src",
    "embed": "src",
    "object": "data",
}
SAFE_EXTERNAL_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rtf",
    ".svg",
    ".text",
    ".tif",
    ".tiff",
    ".tsv",
    ".txt",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
}
SKIP_EXTERNAL_HOST_KEYWORDS = {
    "drive.google.com",
    "docs.google.com",
    "youtube.com",
    "youtu.be",
    "panopto",
    "kaltura",
    "kanopy",
    "library.harvard.edu",
    "hollis.harvard.edu",
    "ezproxy",
    "proxy",
}
MAX_EXTERNAL_BYTES = 250 * 1024 * 1024


class CanvasAPIError(Exception):
    """Raised when Canvas returns an error response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CanvasPermissionError(CanvasAPIError):
    """Raised when Canvas denies the request."""


@dataclass
class ResourceReference:
    tag_name: str
    attr: str
    url: str


def normalize_domain(domain: str) -> str:
    """Return a scheme + host Canvas domain without a trailing slash."""
    value = (domain or "").strip()
    if not value:
        raise ValueError("Canvas domain is required.")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Could not understand Canvas domain: {domain!r}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def parse_course_url(course_url: str) -> tuple[str, str]:
    """Extract the Canvas domain and course id from a Canvas course URL."""
    value = (course_url or "").strip()
    if not value:
        raise ValueError("Canvas course URL is required.")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    match = re.search(r"/courses/(\d+)(?:/|$)", parsed.path)
    if not parsed.netloc or not match:
        raise ValueError(
            "Course URL should look like https://canvas.example.edu/courses/12345"
        )
    return normalize_domain(f"{parsed.scheme}://{parsed.netloc}"), match.group(1)


def sanitize_filename(name: Any, max_length: int = 140) -> str:
    """Make a string safe for use as a local file or folder name."""
    text = str(name or "untitled")
    text = unquote(text)
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "untitled"
    if len(text) <= max_length:
        return text

    suffix = "".join(Path(text).suffixes)
    if suffix and len(suffix) < 20:
        base = text[: max_length - len(suffix)].rstrip(" ._")
        return f"{base}{suffix}"
    return text[:max_length].rstrip(" ._")


def dedupe_filename(filename: Any, used_names: set[str]) -> str:
    """Return a sanitized filename that does not collide with used_names."""
    safe_name = sanitize_filename(filename)
    path = Path(safe_name)
    suffix = path.suffix
    stem = path.stem if suffix else safe_name
    candidate = safe_name
    counter = 2
    lowered = {name.lower() for name in used_names}
    while candidate.lower() in lowered:
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    used_names.add(candidate)
    return candidate


def unique_path(directory: Path, filename: Any, used_names: set[str] | None = None) -> Path:
    """Return a unique path in directory, considering existing files and memory."""
    used = used_names if used_names is not None else set()
    while True:
        candidate = directory / dedupe_filename(filename, used)
        if not candidate.exists():
            return candidate
        filename = candidate.name


def parse_link_header(header: str | None) -> dict[str, str]:
    """Parse a Canvas pagination Link header into {rel: url}."""
    links: dict[str, str] = {}
    if not header:
        return links

    for part in header.split(","):
        section = part.strip()
        if not section.startswith("<") or ">" not in section:
            continue
        url, rest = section[1:].split(">", 1)
        rel = None
        for param in rest.split(";"):
            key, _, value = param.strip().partition("=")
            if key.lower() == "rel":
                rel = value.strip('"')
                break
        if rel:
            links[rel] = url
    return links


def extract_html_resources(html: str) -> list[ResourceReference]:
    """Extract linked and embedded resources from a Canvas HTML fragment."""
    soup = BeautifulSoup(html or "", "html.parser")
    resources: list[ResourceReference] = []
    for tag_name, attr in RESOURCE_ATTRS.items():
        for tag in soup.find_all(tag_name):
            value = tag.get(attr)
            if value:
                resources.append(ResourceReference(tag_name, attr, str(value)))
    return resources


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("Use true or false.")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def rel_link(from_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target, from_dir)).as_posix()


def html_document(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #5f6b7a;
      --border: #d8dee8;
      --accent: #1d6f82;
      --warn: #9a5a00;
      --fail: #9f1d2f;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}
    h1, h2, h3 {{
      line-height: 1.2;
      margin: 0 0 12px;
    }}
    h1 {{
      font-size: 2rem;
    }}
    h2 {{
      border-bottom: 1px solid var(--border);
      font-size: 1.35rem;
      padding-bottom: 8px;
      margin-top: 32px;
    }}
    a {{
      color: var(--accent);
      overflow-wrap: anywhere;
    }}
    .meta, .section {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
      margin: 18px 0;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
      margin: 0;
    }}
    .grid dt {{
      color: var(--muted);
      font-size: 0.85rem;
      margin-top: 8px;
    }}
    .grid dd {{
      margin: 0 0 8px;
      font-weight: 600;
    }}
    ul {{
      padding-left: 1.2rem;
    }}
    li {{
      margin: 7px 0;
    }}
    .modules {{
      border-left: 4px solid var(--accent);
    }}
    .muted {{
      color: var(--muted);
    }}
    .warning {{
      color: var(--warn);
    }}
    .failed {{
      color: var(--fail);
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
    }}
    th, td {{
      border-bottom: 1px solid var(--border);
      padding: 8px;
      text-align: left;
      vertical-align: top;
    }}
    code {{
      background: #eef2f7;
      border-radius: 4px;
      padding: 2px 4px;
    }}
    img, video, iframe {{
      max-width: 100%;
    }}
    @media (max-width: 640px) {{
      main {{
        padding: 20px 12px 40px;
      }}
      h1 {{
        font-size: 1.55rem;
      }}
      .meta, .section {{
        padding: 14px;
      }}
    }}
  </style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


def canvas_fragment_document(title: str, body_html: str, source_url: str | None = None) -> str:
    source = ""
    if source_url:
        source = f'<p class="muted">Original Canvas URL: <a href="{escape(source_url)}">{escape(source_url)}</a></p>'
    body = f"""
<h1>{escape(title)}</h1>
{source}
<article class="section">
{body_html or '<p class="muted">No body content was returned by the Canvas API.</p>'}
</article>
"""
    return html_document(title, body)


class CanvasClient:
    """Small Canvas REST API client with retries and pagination."""

    def __init__(
        self,
        domain: str,
        token: str,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.domain = normalize_domain(domain)
        self.base_url = f"{self.domain}/api/v1"
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "Canvas-Course-Downloader/1.0",
            }
        )

    def url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
        return f"{self.base_url}{path}"

    def get(self, path_or_url: str, params: Any = None, stream: bool = False) -> requests.Response:
        url = self.url(path_or_url)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                    stream=stream,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise CanvasAPIError(f"Could not connect to Canvas: {exc}") from exc

            if response.status_code in TRANSIENT_STATUS_CODES and attempt < self.max_retries:
                delay = self._retry_delay(response, attempt)
                print(
                    f"Canvas returned {response.status_code}; retrying in {delay:.0f} seconds...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue

            if response.status_code in {401, 403}:
                message = self._permission_message(response)
                print(message, file=sys.stderr)
                raise CanvasPermissionError(message, response.status_code)

            if response.status_code >= 400:
                raise CanvasAPIError(self._error_message(response), response.status_code)

            return response

        raise CanvasAPIError(f"Canvas request failed: {last_error}")

    def get_json(self, path_or_url: str, params: Any = None) -> Any:
        response = self.get(path_or_url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise CanvasAPIError(f"Canvas returned non-JSON content for {response.url}") from exc

    def paginate(self, path_or_url: str, params: Any = None) -> list[Any]:
        url = self.url(path_or_url)
        all_items: list[Any] = []
        first = True
        while url:
            response = self.get(url, params=params if first else None)
            first = False
            payload = response.json()
            if isinstance(payload, list):
                all_items.extend(payload)
            else:
                all_items.append(payload)
            links = parse_link_header(response.headers.get("Link"))
            url = links.get("next")
        return all_items

    def download_stream(self, url: str, destination: Path, label: str | None = None) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self.get(url, stream=True)
        total = int(response.headers.get("content-length") or 0)
        desc = label or destination.name
        with destination.open("wb") as handle:
            with tqdm(
                total=total or None,
                unit="B",
                unit_scale=True,
                desc=desc[:40],
                leave=False,
            ) as progress:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        handle.write(chunk)
                        progress.update(len(chunk))

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                pass
        return float(min(30, 2**attempt))

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        detail = response.text[:500].strip()
        return f"Canvas request failed with HTTP {response.status_code} for {response.url}. {detail}"

    @staticmethod
    def _permission_message(response: requests.Response) -> str:
        if response.status_code == 401:
            return (
                "Canvas rejected the API token (401 Unauthorized). Check that the token is copied "
                "correctly and has not expired or been revoked."
            )
        return (
            "Canvas denied access (403 Forbidden). Your token is valid, but this account may not "
            "have permission to view that course item."
        )


class ArchiveLog:
    """Collects archive records, warnings, external links, and failures."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.external_links: list[dict[str, Any]] = []
        self.failed_downloads: list[dict[str, Any]] = []

    def log(
        self,
        canvas_type: str,
        original_url: str | None,
        local_path: Path | None,
        status: str,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "canvas_type": canvas_type,
            "original_url": original_url,
            "local_path": str(local_path) if local_path else None,
            "status": status,
        }
        if error:
            record["error"] = error
        if extra:
            record.update(extra)
        self.records.append(record)
        if status == "failed":
            self.failed_downloads.append(record)

    def warn(self, message: str) -> None:
        print(f"Warning: {message}", file=sys.stderr)
        self.warnings.append(message)

    def external(
        self,
        url: str,
        source_type: str,
        source_title: str,
        reason: str = "not downloaded",
    ) -> None:
        self.external_links.append(
            {
                "url": url,
                "source_type": source_type,
                "source_title": source_title,
                "reason": reason,
            }
        )

    def write(self, course_dir: Path) -> None:
        write_json(course_dir / "archive_log.json", self.records)
        write_json(course_dir / "external_links.json", self.external_links)
        write_json(course_dir / "failed_downloads.json", self.failed_downloads)
        warnings = "\n".join(self.warnings) if self.warnings else "No warnings recorded.\n"
        write_text(course_dir / "archive_warnings.txt", warnings)


class CourseArchiver:
    """Coordinates the Canvas course archive workflow."""

    def __init__(
        self,
        client: CanvasClient,
        course_id: str,
        output_dir: Path,
        include_submissions: bool = False,
        download_external: bool = False,
    ) -> None:
        self.client = client
        self.course_id = str(course_id)
        self.output_dir = output_dir
        self.include_submissions = include_submissions
        self.download_external = download_external
        self.course_url = f"{self.client.domain}/courses/{self.course_id}"
        self.archive_date = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self.log = ArchiveLog()
        self.course: dict[str, Any] = {}
        self.course_dir: Path = output_dir / self.course_id
        self.used_names_by_dir: dict[str, set[str]] = {}
        self.files_index: list[dict[str, Any]] = []
        self.pages_index: list[dict[str, Any]] = []
        self.assignments_index: list[dict[str, Any]] = []
        self.modules_index: list[dict[str, Any]] = []
        self.submissions_index: list[dict[str, Any]] = []
        self.syllabus_path: Path | None = None

    def archive(self) -> Path:
        print(f"Fetching course metadata for course {self.course_id}...")
        self.course = self.fetch_course()
        self.course_dir = self.output_dir / self.course_folder_name(self.course)
        self.course_dir.mkdir(parents=True, exist_ok=True)

        self.save_course_metadata()
        self.save_syllabus()
        self.archive_files()
        self.archive_pages()
        self.archive_assignments()
        self.archive_modules()
        if self.include_submissions:
            self.archive_submissions()
        write_json(self.course_dir / "files" / "files_index.json", self.files_index)
        self.write_course_index()
        self.log.write(self.course_dir)
        return self.course_dir

    def fetch_course(self) -> dict[str, Any]:
        params = [("include[]", "syllabus_body")]
        try:
            course = self.client.get_json(f"/courses/{self.course_id}", params=params)
        except CanvasAPIError as exc:
            if exc.status_code != 400:
                raise
            self.log.warn(
                "Canvas did not accept include[]=syllabus_body; retrying course metadata without it."
            )
            course = self.client.get_json(f"/courses/{self.course_id}")
        if not isinstance(course, dict):
            raise CanvasAPIError("Canvas returned an unexpected course metadata response.")
        return course

    @staticmethod
    def course_folder_name(course: dict[str, Any]) -> str:
        course_id = course.get("id") or "course"
        name = course.get("name") or course.get("course_code") or f"Canvas Course {course_id}"
        return f"{course_id} - {sanitize_filename(name, 90)}"

    def save_course_metadata(self) -> None:
        summary = {
            "id": self.course.get("id"),
            "name": self.course.get("name"),
            "course_code": self.course.get("course_code"),
            "workflow_state": self.course.get("workflow_state"),
            "default_view": self.course.get("default_view"),
            "syllabus_body": self.course.get("syllabus_body"),
        }
        write_json(self.course_dir / "course.json", self.course)
        rows = "".join(
            f"<tr><th>{escape(str(key))}</th><td>{escape(str(value or ''))}</td></tr>"
            for key, value in summary.items()
            if key != "syllabus_body"
        )
        body = f"""
<h1>{escape(self.course.get('name') or 'Canvas Course')}</h1>
<div class="section">
  <table>{rows}</table>
</div>
"""
        write_text(self.course_dir / "course_summary.html", html_document("Course Summary", body))
        self.log.log(
            "course",
            self.client.url(f"/courses/{self.course_id}"),
            self.course_dir / "course.json",
            "saved",
        )

    def save_syllabus(self) -> None:
        syllabus_dir = self.course_dir / "syllabus"
        syllabus_dir.mkdir(parents=True, exist_ok=True)
        syllabus_body = self.course.get("syllabus_body") or ""
        if syllabus_body.strip():
            path = syllabus_dir / "syllabus.html"
            linked_dir = self.course_dir / "linked_files" / "syllabus"
            rewritten = self.rewrite_and_download_resources(
                syllabus_body,
                path,
                linked_dir,
                "syllabus",
                "Syllabus",
            )
            write_text(
                path,
                canvas_fragment_document("Syllabus", rewritten, f"{self.course_url}/assignments/syllabus"),
            )
            self.syllabus_path = path
            self.log.log("syllabus", f"{self.course_url}/assignments/syllabus", path, "saved")
            return

        path = syllabus_dir / "README.txt"
        write_text(
            path,
            "No syllabus body was returned by the Canvas API for this course.\n",
        )
        self.syllabus_path = path
        self.log.log("syllabus", self.client.url(f"/courses/{self.course_id}"), path, "empty")

    def archive_files(self) -> None:
        print("Downloading course files...")
        files_dir = self.course_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        try:
            files = self.client.paginate(f"/courses/{self.course_id}/files", params={"per_page": 100})
        except CanvasAPIError as exc:
            self.log.warn(f"Could not list course files: {exc}")
            self.log.log("file", self.client.url(f"/courses/{self.course_id}/files"), None, "failed", str(exc))
            write_json(files_dir / "files_index.json", self.files_index)
            return

        for file_info in tqdm(files, desc="Course files"):
            self.download_file_metadata(file_info, files_dir, "file")

        write_json(files_dir / "files_index.json", self.files_index)

    def archive_pages(self) -> None:
        print("Downloading Canvas pages...")
        pages_dir = self.course_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        try:
            pages = self.client.paginate(f"/courses/{self.course_id}/pages", params={"per_page": 100})
        except CanvasAPIError as exc:
            self.log.warn(f"Could not list Canvas pages: {exc}")
            self.log.log("page", self.client.url(f"/courses/{self.course_id}/pages"), None, "failed", str(exc))
            write_json(pages_dir / "pages_index.json", self.pages_index)
            return

        for index, page in enumerate(tqdm(pages, desc="Pages"), start=1):
            self.archive_one_page(page, pages_dir, index, "page")

        write_json(pages_dir / "pages_index.json", self.pages_index)

    def archive_one_page(
        self,
        page: dict[str, Any],
        pages_dir: Path,
        index: int,
        canvas_type: str,
    ) -> Path | None:
        page_url = page.get("url") or page.get("page_id") or page.get("id")
        title = page.get("title") or page_url or f"Page {index}"
        slug = sanitize_filename(page.get("url") or f"{index:03d}", 50)
        filename = f"{slug} - {sanitize_filename(title, 80)}.html"
        path = unique_path(pages_dir, filename, self.used_set(pages_dir))
        api_url = self.client.url(f"/courses/{self.course_id}/pages/{quote(str(page_url), safe='')}")
        try:
            detail = self.client.get_json(api_url)
            body = detail.get("body") or ""
            linked_dir = self.course_dir / "linked_files" / "pages"
            rewritten = self.rewrite_and_download_resources(body, path, linked_dir, "page", title)
            write_text(path, canvas_fragment_document(title, rewritten, detail.get("html_url")))
            record = {
                "title": title,
                "url": page_url,
                "page_id": detail.get("page_id"),
                "local_path": str(path),
                "html_url": detail.get("html_url"),
            }
            self.pages_index.append(record)
            self.log.log(canvas_type, api_url, path, "saved", extra={"title": title})
            return path
        except Exception as exc:
            self.log.warn(f"Could not save page {title}: {exc}")
            self.log.log(canvas_type, api_url, path, "failed", str(exc), {"title": title})
            self.pages_index.append({"title": title, "url": page_url, "local_path": None, "error": str(exc)})
            return None

    def archive_assignments(self) -> None:
        print("Downloading assignments...")
        assignments_dir = self.course_dir / "assignments"
        assignments_dir.mkdir(parents=True, exist_ok=True)
        try:
            assignments = self.client.paginate(
                f"/courses/{self.course_id}/assignments",
                params={"per_page": 100},
            )
        except CanvasAPIError as exc:
            self.log.warn(f"Could not list assignments: {exc}")
            self.log.log(
                "assignment",
                self.client.url(f"/courses/{self.course_id}/assignments"),
                None,
                "failed",
                str(exc),
            )
            write_json(assignments_dir / "assignments_index.json", self.assignments_index)
            return

        for index, assignment in enumerate(tqdm(assignments, desc="Assignments"), start=1):
            self.archive_one_assignment(assignment, assignments_dir, index, "assignment")

        write_json(assignments_dir / "assignments_index.json", self.assignments_index)

    def archive_one_assignment(
        self,
        assignment: dict[str, Any],
        assignments_dir: Path,
        index: int,
        canvas_type: str,
        record_index: bool = True,
    ) -> Path | None:
        assignment_id = assignment.get("id") or assignment.get("content_id")
        name = assignment.get("name") or assignment.get("title") or f"Assignment {assignment_id or index}"
        position = assignment.get("position") or index
        path = unique_path(
            assignments_dir,
            f"{int(position):03d} - {sanitize_filename(name, 90)}.html"
            if str(position).isdigit()
            else f"{sanitize_filename(position, 40)} - {sanitize_filename(name, 90)}.html",
            self.used_set(assignments_dir),
        )
        api_url = self.client.url(f"/courses/{self.course_id}/assignments/{assignment_id}")
        try:
            detail = assignment
            if assignment_id:
                detail = self.client.get_json(api_url)
            description = detail.get("description") or ""
            linked_dir = self.course_dir / "linked_files" / "assignments"
            rewritten = self.rewrite_and_download_resources(
                description,
                path,
                linked_dir,
                "assignment",
                name,
            )
            assignment_meta = self.assignment_metadata_html(detail)
            body = f"{assignment_meta}<article class=\"section\">{rewritten or '<p class=\"muted\">No assignment description was returned.</p>'}</article>"
            write_text(path, html_document(name, f"<h1>{escape(name)}</h1>{body}"))
            record = {
                "id": assignment_id,
                "name": name,
                "position": position,
                "local_path": str(path),
                "html_url": detail.get("html_url"),
                "due_at": detail.get("due_at"),
                "points_possible": detail.get("points_possible"),
            }
            if record_index:
                self.assignments_index.append(record)
            self.log.log(canvas_type, api_url, path, "saved", extra={"title": name})
            return path
        except Exception as exc:
            self.log.warn(f"Could not save assignment {name}: {exc}")
            self.log.log(canvas_type, api_url, path, "failed", str(exc), {"title": name})
            if record_index:
                self.assignments_index.append(
                    {"id": assignment_id, "name": name, "local_path": None, "error": str(exc)}
                )
            return None

    @staticmethod
    def assignment_metadata_html(assignment: dict[str, Any]) -> str:
        fields = [
            ("Due", assignment.get("due_at")),
            ("Unlocks", assignment.get("unlock_at")),
            ("Locks", assignment.get("lock_at")),
            ("Points", assignment.get("points_possible")),
            ("Submission types", ", ".join(assignment.get("submission_types") or [])),
            ("Canvas URL", assignment.get("html_url")),
        ]
        rows = "".join(
            f"<tr><th>{escape(label)}</th><td>{escape(str(value))}</td></tr>"
            for label, value in fields
            if value not in (None, "")
        )
        return f'<div class="section"><table>{rows}</table></div>' if rows else ""

    def archive_modules(self) -> None:
        print("Downloading modules and module items...")
        modules_root = self.course_dir / "modules"
        modules_root.mkdir(parents=True, exist_ok=True)
        try:
            modules = self.client.paginate(f"/courses/{self.course_id}/modules", params={"per_page": 100})
        except CanvasAPIError as exc:
            self.log.warn(f"Could not list modules: {exc}")
            self.log.log("module", self.client.url(f"/courses/{self.course_id}/modules"), None, "failed", str(exc))
            write_json(modules_root / "modules_index.json", self.modules_index)
            return

        for module_index, module in enumerate(tqdm(modules, desc="Modules"), start=1):
            self.archive_one_module(module, modules_root, module_index)

        write_json(modules_root / "modules_index.json", self.modules_index)

    def archive_one_module(self, module: dict[str, Any], modules_root: Path, module_index: int) -> None:
        module_name = module.get("name") or f"Module {module_index}"
        module_dir = unique_path(
            modules_root,
            f"{module_index:02d} - {sanitize_filename(module_name, 90)}",
            self.used_set(modules_root),
        )
        module_dir.mkdir(parents=True, exist_ok=True)
        items_url = self.client.url(f"/courses/{self.course_id}/modules/{module.get('id')}/items")
        module_record = {
            "id": module.get("id"),
            "name": module_name,
            "position": module.get("position") or module_index,
            "local_path": str(module_dir / "module_index.html"),
            "items": [],
        }
        try:
            items = self.client.paginate(items_url, params={"per_page": 100})
        except Exception as exc:
            self.log.warn(f"Could not list items for module {module_name}: {exc}")
            self.log.log("module_item", items_url, None, "failed", str(exc), {"module": module_name})
            items = []

        for item_index, item in enumerate(items, start=1):
            item_record = self.handle_module_item(item, module_dir, item_index, module_name)
            module_record["items"].append(item_record)

        write_json(module_dir / "module_items.json", module_record["items"])
        self.write_module_index(module, module_dir, module_record["items"])
        self.modules_index.append(module_record)
        self.log.log(
            "module",
            items_url,
            module_dir / "module_index.html",
            "saved",
            extra={"module": module_name},
        )

    def handle_module_item(
        self,
        item: dict[str, Any],
        module_dir: Path,
        item_index: int,
        module_name: str,
    ) -> dict[str, Any]:
        item_type = item.get("type") or "Unknown"
        title = item.get("title") or f"{item_type} {item_index}"
        position = item.get("position") or item_index
        base_record: dict[str, Any] = {
            "id": item.get("id"),
            "type": item_type,
            "title": title,
            "position": position,
            "html_url": item.get("html_url"),
            "external_url": item.get("external_url"),
            "content_id": item.get("content_id"),
        }
        try:
            if item_type == "Page" and item.get("page_url"):
                path = self.archive_module_page(item, module_dir, item_index)
                base_record["local_path"] = str(path) if path else None
            elif item_type == "File" and item.get("content_id"):
                path = self.download_canvas_file_by_id(
                    str(item["content_id"]),
                    module_dir / "files",
                    "module_item",
                    item.get("url") or item.get("html_url"),
                )
                base_record["local_path"] = str(path) if path else None
            elif item_type == "Assignment" and item.get("content_id"):
                detail = self.client.get_json(
                    f"/courses/{self.course_id}/assignments/{item['content_id']}"
                )
                path = self.archive_one_assignment(
                    detail,
                    module_dir,
                    item_index,
                    "module_item",
                    record_index=False,
                )
                base_record["local_path"] = str(path) if path else None
            elif item_type == "ExternalUrl":
                url = item.get("external_url") or item.get("html_url")
                if url:
                    self.log.external(url, "module_item", title, "external module item")
            else:
                self.log.warn(
                    f"Module item '{title}' has unsupported type '{item_type}'. Metadata was saved."
                )
                self.log.log(
                    "module_item",
                    item.get("html_url") or item.get("url"),
                    None,
                    "metadata_only",
                    extra={"module": module_name, "title": title, "type": item_type},
                )
        except Exception as exc:
            self.log.warn(f"Could not archive module item {title}: {exc}")
            self.log.log(
                "module_item",
                item.get("html_url") or item.get("url"),
                None,
                "failed",
                str(exc),
                {"module": module_name, "title": title, "type": item_type},
            )
            base_record["error"] = str(exc)
        return base_record

    def archive_module_page(self, item: dict[str, Any], module_dir: Path, item_index: int) -> Path | None:
        title = item.get("title") or item.get("page_url") or f"Page {item_index}"
        page_url = item["page_url"]
        path = unique_path(
            module_dir,
            f"{item_index:03d} - {sanitize_filename(title, 90)}.html",
            self.used_set(module_dir),
        )
        api_url = self.client.url(f"/courses/{self.course_id}/pages/{quote(str(page_url), safe='')}")
        detail = self.client.get_json(api_url)
        body = detail.get("body") or ""
        linked_dir = self.course_dir / "linked_files" / "modules"
        rewritten = self.rewrite_and_download_resources(body, path, linked_dir, "module_item", title)
        write_text(path, canvas_fragment_document(title, rewritten, detail.get("html_url")))
        self.log.log("module_item", api_url, path, "saved", extra={"title": title, "type": "Page"})
        return path

    def write_module_index(
        self,
        module: dict[str, Any],
        module_dir: Path,
        item_records: list[dict[str, Any]],
    ) -> None:
        module_name = module.get("name") or "Module"
        items_html = []
        for item in sorted(item_records, key=lambda value: value.get("position") or 0):
            title = item.get("title") or "Untitled"
            item_type = item.get("type") or "Unknown"
            local = item.get("local_path")
            if local:
                link = rel_link(module_dir, Path(local))
                label = f'<a href="{escape(link)}">{escape(title)}</a>'
            elif item.get("external_url"):
                label = f'<a href="{escape(item["external_url"])}">{escape(title)}</a>'
            elif item.get("html_url"):
                label = f'<a href="{escape(item["html_url"])}">{escape(title)}</a>'
            else:
                label = escape(title)
            items_html.append(f"<li><strong>{escape(item_type)}</strong>: {label}</li>")
        body = f"""
<h1>{escape(module_name)}</h1>
<p class="muted">Original Canvas module order is preserved where Canvas returned item positions.</p>
<div class="section modules">
  <ol>
    {''.join(items_html) or '<li>No module items were returned.</li>'}
  </ol>
</div>
"""
        write_text(module_dir / "module_index.html", html_document(module_name, body))

    def archive_submissions(self) -> None:
        print("Attempting to download current user's submissions...")
        submissions_dir = self.course_dir / "submissions"
        submissions_dir.mkdir(parents=True, exist_ok=True)
        if not self.assignments_index:
            self.log.warn("No assignments were available, so submissions were not checked.")
            write_json(submissions_dir / "submissions_index.json", self.submissions_index)
            return

        for assignment in tqdm(self.assignments_index, desc="Submissions"):
            assignment_id = assignment.get("id")
            if not assignment_id:
                continue
            title = assignment.get("name") or f"Assignment {assignment_id}"
            api_path = f"/courses/{self.course_id}/assignments/{assignment_id}/submissions/self"
            try:
                submission = self.client.get_json(
                    api_path,
                    params=[
                        ("include[]", "submission_comments"),
                        ("include[]", "submission_history"),
                    ],
                )
                filename = f"{sanitize_filename(title, 90)} - submission.json"
                metadata_path = unique_path(submissions_dir, filename, self.used_set(submissions_dir))
                write_json(metadata_path, submission)
                record = {
                    "assignment_id": assignment_id,
                    "assignment_name": title,
                    "local_path": str(metadata_path),
                    "workflow_state": submission.get("workflow_state"),
                    "attempt": submission.get("attempt"),
                    "attachments": [],
                }
                body = submission.get("body")
                if body:
                    body_path = metadata_path.with_suffix(".html")
                    write_text(body_path, canvas_fragment_document(f"{title} Submission", body))
                    record["body_local_path"] = str(body_path)
                for attachment in submission.get("attachments") or []:
                    path = self.download_submission_attachment(attachment, submissions_dir / "attachments")
                    if path:
                        record["attachments"].append(str(path))
                self.submissions_index.append(record)
                self.log.log("submission", self.client.url(api_path), metadata_path, "saved")
            except CanvasPermissionError as exc:
                message = f"Submission for assignment {title} was not available: {exc}"
                self.log.warn(message)
                self.log.log("submission", self.client.url(api_path), None, "failed", message)
            except Exception as exc:
                message = f"Could not fetch submission for assignment {title}: {exc}"
                self.log.warn(message)
                self.log.log("submission", self.client.url(api_path), None, "failed", message)

        write_json(submissions_dir / "submissions_index.json", self.submissions_index)

    def download_submission_attachment(self, attachment: dict[str, Any], dest_dir: Path) -> Path | None:
        url = attachment.get("url")
        filename = attachment.get("filename") or attachment.get("display_name") or f"attachment-{attachment.get('id')}"
        if not url:
            self.log.warn(f"Submission attachment {filename} did not include a download URL.")
            return None
        path = unique_path(dest_dir, filename, self.used_set(dest_dir))
        try:
            self.client.download_stream(url, path, path.name)
            self.log.log("submission", url, path, "downloaded")
            return path
        except Exception as exc:
            self.log.warn(f"Could not download submission attachment {filename}: {exc}")
            self.log.log("submission", url, path, "failed", str(exc))
            return None

    def rewrite_and_download_resources(
        self,
        html: str,
        document_path: Path,
        linked_dir: Path,
        source_type: str,
        source_title: str,
    ) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag_name, attr in RESOURCE_ATTRS.items():
            for tag in soup.find_all(tag_name):
                raw_url = tag.get(attr)
                if not raw_url:
                    continue
                absolute_url = self.absolute_resource_url(str(raw_url))
                local_path = self.download_resource(
                    absolute_url,
                    linked_dir,
                    source_type,
                    source_title,
                )
                if local_path:
                    tag[attr] = rel_link(document_path.parent, local_path)
        return str(soup)

    def absolute_resource_url(self, raw_url: str) -> str:
        value = raw_url.strip()
        if value.startswith("#") or value.startswith("mailto:") or value.startswith("tel:"):
            return value
        return urljoin(f"{self.client.domain}/", value)

    def download_resource(
        self,
        url: str,
        linked_dir: Path,
        source_type: str,
        source_title: str,
    ) -> Path | None:
        file_id = self.extract_canvas_file_id(url)
        if file_id:
            return self.download_canvas_file_by_id(file_id, linked_dir, source_type, url)

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None
        if self.same_canvas_host(url):
            self.log.external(url, source_type, source_title, "Canvas URL was not a recognized file URL")
            return None

        if not self.download_external:
            self.log.external(url, source_type, source_title, "external download disabled")
            return None
        return self.download_external_resource(url, linked_dir, source_type, source_title)

    def download_file_metadata(
        self,
        file_info: dict[str, Any],
        dest_dir: Path,
        canvas_type: str,
        source_url: str | None = None,
    ) -> Path | None:
        filename = (
            file_info.get("filename")
            or file_info.get("display_name")
            or file_info.get("name")
            or f"file-{file_info.get('id', 'unknown')}"
        )
        download_url = file_info.get("download_url") or file_info.get("url")
        original_url = source_url or download_url or self.client.url(f"/files/{file_info.get('id')}")
        path = unique_path(dest_dir, filename, self.used_set(dest_dir))
        record = dict(file_info)
        record["local_path"] = str(path)
        if not download_url:
            error = f"No download URL returned for file {filename}"
            self.log.warn(error)
            self.log.log(canvas_type, original_url, path, "failed", error)
            record["status"] = "failed"
            record["error"] = error
            self.files_index.append(record)
            return None
        try:
            self.client.download_stream(download_url, path, path.name)
            self.log.log(canvas_type, original_url, path, "downloaded")
            record["status"] = "downloaded"
            self.files_index.append(record)
            return path
        except Exception as exc:
            self.log.warn(f"Could not download file {filename}: {exc}")
            self.log.log(canvas_type, original_url, path, "failed", str(exc))
            record["status"] = "failed"
            record["error"] = str(exc)
            self.files_index.append(record)
            return None

    def download_canvas_file_by_id(
        self,
        file_id: str,
        dest_dir: Path,
        canvas_type: str,
        source_url: str | None = None,
    ) -> Path | None:
        api_url = self.client.url(f"/files/{file_id}")
        try:
            file_info = self.client.get_json(api_url)
            return self.download_file_metadata(file_info, dest_dir, canvas_type, source_url or api_url)
        except Exception as exc:
            self.log.warn(f"Could not download Canvas file ID {file_id}: {exc}")
            self.log.log(canvas_type, source_url or api_url, None, "failed", str(exc))
            return None

    def download_external_resource(
        self,
        url: str,
        linked_dir: Path,
        source_type: str,
        source_title: str,
    ) -> Path | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if any(keyword in host for keyword in SKIP_EXTERNAL_HOST_KEYWORDS):
            self.log.external(url, source_type, source_title, "protected or streaming host skipped")
            return None

        ext = Path(parsed.path).suffix.lower()
        if ext not in SAFE_EXTERNAL_EXTENSIONS:
            self.log.external(url, source_type, source_title, "not a safe static file extension")
            return None

        filename = sanitize_filename(Path(parsed.path).name or f"external{ext}")
        path = unique_path(linked_dir, filename, self.used_set(linked_dir))
        linked_dir.mkdir(parents=True, exist_ok=True)
        try:
            with requests.get(url, timeout=30, stream=True, allow_redirects=True) as response:
                if response.status_code >= 400:
                    raise CanvasAPIError(f"HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";")[0].lower()
                if content_type in {"text/html", "application/javascript", "text/javascript"}:
                    self.log.external(url, source_type, source_title, f"content-type {content_type} skipped")
                    return None
                total = int(response.headers.get("content-length") or 0)
                if total > MAX_EXTERNAL_BYTES:
                    self.log.external(url, source_type, source_title, "external file too large")
                    return None
                written = 0
                with path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 128):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_EXTERNAL_BYTES:
                            raise CanvasAPIError("external file exceeded size limit")
                        handle.write(chunk)
            self.log.log(source_type, url, path, "downloaded_external")
            return path
        except Exception as exc:
            self.log.external(url, source_type, source_title, f"external download failed: {exc}")
            self.log.log(source_type, url, path, "failed", str(exc))
            return None

    def extract_canvas_file_id(self, url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.netloc and not self.same_canvas_host(url):
            return None
        path = parsed.path
        patterns = [
            r"/api/v1/(?:courses/\d+/)?files/(\d+)(?:/|$)",
            r"/(?:courses/\d+/)?files/(\d+)(?:/|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, path)
            if match:
                return match.group(1)
        return None

    def same_canvas_host(self, url: str) -> bool:
        parsed = urlparse(url)
        canvas_host = urlparse(self.client.domain).netloc.lower()
        return parsed.netloc.lower() == canvas_host

    def used_set(self, directory: Path) -> set[str]:
        key = str(directory)
        if key not in self.used_names_by_dir:
            directory.mkdir(parents=True, exist_ok=True)
            self.used_names_by_dir[key] = {path.name for path in directory.iterdir()}
        return self.used_names_by_dir[key]

    def write_course_index(self) -> None:
        course_name = self.course.get("name") or "Canvas Course"
        code = self.course.get("course_code") or ""
        syllabus_link = (
            f'<a href="{escape(rel_link(self.course_dir, self.syllabus_path))}">Syllabus</a>'
            if self.syllabus_path
            else '<span class="muted">No syllabus file created.</span>'
        )
        body = f"""
<h1>{escape(course_name)}</h1>
<div class="meta">
  <dl class="grid">
    <div><dt>Course code</dt><dd>{escape(str(code))}</dd></div>
    <div><dt>Canvas course</dt><dd><a href="{escape(self.course_url)}">{escape(self.course_url)}</a></dd></div>
    <div><dt>Archived</dt><dd>{escape(self.archive_date)}</dd></div>
    <div><dt>Default view</dt><dd>{escape(str(self.course.get('default_view') or ''))}</dd></div>
  </dl>
</div>
{self.index_section('Syllabus', f'<p>{syllabus_link}</p>' + self.syllabus_preview())}
{self.modules_section()}
{self.link_list_section('Pages', self.pages_index, self.course_dir)}
{self.link_list_section('Assignments', self.assignments_index, self.course_dir)}
{self.link_list_section('Files', self.files_index, self.course_dir, title_key='filename')}
{self.link_list_section('Submissions', self.submissions_index, self.course_dir, title_key='assignment_name') if self.include_submissions else ''}
{self.external_links_section()}
{self.warnings_section()}
"""
        write_text(self.course_dir / "index.html", html_document(course_name, body))

    def syllabus_preview(self) -> str:
        syllabus_body = self.course.get("syllabus_body") or ""
        if not syllabus_body.strip():
            return '<p class="muted">Canvas did not return a syllabus body.</p>'
        return f'<div class="section">{syllabus_body}</div>'

    @staticmethod
    def index_section(title: str, content: str) -> str:
        return f'<section class="section"><h2>{escape(title)}</h2>{content}</section>'

    def modules_section(self) -> str:
        items = []
        for module in sorted(self.modules_index, key=lambda value: value.get("position") or 0):
            path = module.get("local_path")
            name = module.get("name") or "Module"
            count = len(module.get("items") or [])
            if path:
                link = f'<a href="{escape(rel_link(self.course_dir, Path(path)))}">{escape(name)}</a>'
            else:
                link = escape(name)
            items.append(f"<li>{link} <span class=\"muted\">({count} items)</span></li>")
        content = f"<ol>{''.join(items) or '<li>No modules were returned by Canvas.</li>'}</ol>"
        return f'<section class="section modules"><h2>Modules</h2>{content}</section>'

    def link_list_section(
        self,
        title: str,
        records: list[dict[str, Any]],
        base_dir: Path,
        title_key: str = "title",
    ) -> str:
        items = []
        for record in records:
            label = (
                record.get(title_key)
                or record.get("name")
                or record.get("display_name")
                or record.get("id")
                or "Untitled"
            )
            path = record.get("local_path")
            if path:
                items.append(f'<li><a href="{escape(rel_link(base_dir, Path(path)))}">{escape(str(label))}</a></li>')
            elif record.get("html_url"):
                items.append(f'<li><a href="{escape(record["html_url"])}">{escape(str(label))}</a></li>')
            else:
                items.append(f"<li>{escape(str(label))}</li>")
        return self.index_section(title, f"<ul>{''.join(items) or '<li>None saved.</li>'}</ul>")

    def external_links_section(self) -> str:
        items = []
        for link in self.log.external_links:
            label = f"{link.get('source_title') or link.get('source_type')}: {link.get('url')}"
            reason = link.get("reason") or "not downloaded"
            items.append(
                f'<li><a href="{escape(link["url"])}">{escape(label)}</a> '
                f'<span class="muted">({escape(reason)})</span></li>'
            )
        return self.index_section(
            "External Links Not Downloaded",
            f"<ul>{''.join(items) or '<li>No external links recorded.</li>'}</ul>",
        )

    def warnings_section(self) -> str:
        warnings = [f'<li class="warning">{escape(message)}</li>' for message in self.log.warnings]
        failures = [
            f'<li class="failed">{escape(str(item.get("original_url") or ""))}: '
            f'{escape(str(item.get("error") or item.get("status") or ""))}</li>'
            for item in self.log.failed_downloads
        ]
        content = (
            f"<h3>Warnings</h3><ul>{''.join(warnings) or '<li>No warnings recorded.</li>'}</ul>"
            f"<h3>Failed Downloads</h3><ul>{''.join(failures) or '<li>No failed downloads recorded.</li>'}</ul>"
        )
        return self.index_section("Warnings And Failures", content)


def run_dry_run(client: CanvasClient, course_id: str) -> int:
    print(f"Dry run for {client.domain}/courses/{course_id}")
    try:
        course = client.get_json(f"/courses/{course_id}", params=[("include[]", "syllabus_body")])
    except CanvasAPIError as exc:
        if exc.status_code != 400:
            raise
        course = client.get_json(f"/courses/{course_id}")
    print(f"Course: {course.get('name') or course.get('course_code') or course_id}")
    print(f"default_view: {course.get('default_view')}")
    print(f"syllabus_body returned: {'yes' if (course.get('syllabus_body') or '').strip() else 'no'}")
    counts = {
        "files": f"/courses/{course_id}/files",
        "pages": f"/courses/{course_id}/pages",
        "modules": f"/courses/{course_id}/modules",
        "assignments": f"/courses/{course_id}/assignments",
    }
    for label, path in counts.items():
        try:
            items = client.paginate(path, params={"per_page": 100})
            print(f"{label}: {len(items)}")
        except CanvasAPIError as exc:
            print(f"{label}: unavailable ({exc})")
    print("Dry run complete. No files were downloaded.")
    return 0


def write_global_dashboard(output_dir: Path, course_dirs: Iterable[Path]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for course_dir in course_dirs:
        course_json = course_dir / "course.json"
        title = course_dir.name
        code = ""
        if course_json.exists():
            try:
                course = json.loads(course_json.read_text(encoding="utf-8"))
                title = course.get("name") or title
                code = course.get("course_code") or ""
            except (OSError, ValueError):
                pass
        link = rel_link(output_dir, course_dir / "index.html")
        label = f"{title} ({code})" if code else title
        items.append(f'<li><a href="{escape(link)}">{escape(label)}</a></li>')
    body = f"""
<h1>Canvas Course Archives</h1>
<div class="section">
  <ul>
    {''.join(items) or '<li>No course archives found.</li>'}
  </ul>
</div>
"""
    write_text(output_dir / "index.html", html_document("Canvas Course Archives", body))


def token_missing_message(token_env: str) -> str:
    return f"""Canvas API token was not found.

The script looks for your token in the {token_env} environment variable.

macOS/Linux:
  export {token_env}="your_token_here"

Windows PowerShell:
  $env:{token_env}="your_token_here"

Then run this command again. Treat the token like a password and revoke it in
Canvas when you no longer need it.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive one Canvas course using the Canvas REST API.",
    )
    parser.add_argument("--course-url", help="Canvas course URL, for example https://canvas.harvard.edu/courses/146553")
    parser.add_argument("--domain", help="Canvas domain, for example https://canvas.harvard.edu")
    parser.add_argument("--course-id", help="Canvas course id, for example 146553")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help=f"Environment variable containing the token. Default: {DEFAULT_TOKEN_ENV}")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"Archive output directory. Default: {DEFAULT_OUTPUT_DIR}")
    parser.add_argument("--include-submissions", action="store_true", help="Attempt to download the current user's submissions.")
    parser.add_argument("--download-external", type=parse_bool, default=False, help="Download direct static external files. Use true or false. Default: false")
    parser.add_argument("--dry-run", action="store_true", help="Fetch metadata and counts without downloading content.")
    return parser


def resolve_course_args(args: argparse.Namespace) -> tuple[str, str]:
    if args.course_url:
        return parse_course_url(args.course_url)
    if args.domain and args.course_id:
        return normalize_domain(args.domain), str(args.course_id)
    raise ValueError("Provide either --course-url or both --domain and --course-id.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        domain, course_id = resolve_course_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    token = os.environ.get(args.token_env)
    if not token:
        print(token_missing_message(args.token_env), file=sys.stderr)
        return 2

    client = CanvasClient(domain, token)
    try:
        if args.dry_run:
            return run_dry_run(client, course_id)

        output_dir = Path(args.output_dir).expanduser().resolve()
        archiver = CourseArchiver(
            client,
            course_id,
            output_dir,
            include_submissions=args.include_submissions,
            download_external=args.download_external,
        )
        course_dir = archiver.archive()
        write_global_dashboard(output_dir, [course_dir])
        print(f"Archive complete: {course_dir}")
        print(f"Open the local course navigation page: {course_dir / 'index.html'}")
        print(f"Top-level dashboard: {output_dir / 'index.html'}")
        return 0
    except CanvasPermissionError:
        return 1
    except CanvasAPIError as exc:
        print(f"Canvas API error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Archive cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
