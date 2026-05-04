#!/usr/bin/env python3
"""Canvas API archiver.

This script archives either one Canvas Page or one full Canvas course through
the Canvas REST API. It does not scrape browser HTML.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm


DEFAULT_OUTPUT_DIR = "canvas_all_content"
DEFAULT_TOKEN_ENV = "CANVAS_API_TOKEN"
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
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
}
PROTECTED_HOST_KEYWORDS = {
    "box.com",
    "dropbox.com",
    "drive.google.com",
    "docs.google.com",
    "ezproxy",
    "external_tools",
    "hollis.harvard.edu",
    "jstor.org",
    "kaltura",
    "kanopy",
    "library.harvard.edu",
    "login.ezp-prod1.hul.harvard.edu",
    "muse.jhu.edu",
    "panopto",
    "perusall",
    "proquest.com",
    "proxy",
    "sciencedirect.com",
    "springer.com",
    "tandfonline.com",
    "vimeo.com",
    "youtube.com",
    "youtu.be",
}
MAX_EXTERNAL_BYTES = 100 * 1024 * 1024


class CanvasAPIError(Exception):
    """Raised when Canvas or a network call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CanvasPermissionError(CanvasAPIError):
    """Raised for Canvas 401/403 responses."""


@dataclass
class CoursePageTarget:
    domain: str
    course_id: str
    page_slug: str
    original_url: str


@dataclass
class CourseTarget:
    domain: str
    course_id: str
    original_url: str


@dataclass
class ResourceRef:
    tag_name: str
    attr: str
    url: str
    absolute_url: str
    link_text: str
    where_found: str


@dataclass
class ResourceClassification:
    kind: str
    id_or_slug: str | None
    downloadable_by_api: bool
    should_record_only: bool
    reason: str
    normalized_url: str


def normalize_domain(domain: str) -> str:
    value = (domain or "").strip()
    if not value:
        raise ValueError("Canvas domain is required.")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Could not understand Canvas domain: {domain!r}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def parse_course_page_url(url: str) -> CoursePageTarget:
    """Parse a Canvas Page URL into domain, course id, and page slug."""
    value = (url or "").strip()
    if not value:
        raise ValueError("A Canvas course page URL is required.")
    if "://" not in value:
        value = f"https://{value}"

    parsed = urlparse(value)
    match = re.search(r"/courses/(\d+)/pages/([^/?#]+)/*$", parsed.path)
    if not parsed.netloc or not match:
        raise ValueError(
            "Course page URL should look like "
            "https://canvas.example.edu/courses/151500/pages/week-1"
        )

    domain = normalize_domain(f"{parsed.scheme}://{parsed.netloc}")
    return CoursePageTarget(
        domain=domain,
        course_id=match.group(1),
        page_slug=unquote(match.group(2)),
        original_url=value,
    )


def parse_course_url(url: str) -> CourseTarget:
    """Parse a Canvas course URL into domain and course id."""
    value = (url or "").strip()
    if not value:
        raise ValueError("A Canvas course URL is required.")
    if "://" not in value:
        value = f"https://{value}"

    parsed = urlparse(value)
    match = re.search(r"/courses/(\d+)(?:/|$)", parsed.path)
    if not parsed.netloc or not match:
        raise ValueError(
            "Course URL should look like https://canvas.example.edu/courses/151500"
        )

    domain = normalize_domain(f"{parsed.scheme}://{parsed.netloc}")
    course_id = match.group(1)
    return CourseTarget(
        domain=domain,
        course_id=course_id,
        original_url=f"{domain}/courses/{course_id}",
    )


def sanitize_filename(name: Any, max_length: int = 140) -> str:
    text = unquote(str(name or "untitled"))
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
    safe_name = sanitize_filename(filename)
    suffix = Path(safe_name).suffix
    stem = Path(safe_name).stem if suffix else safe_name
    candidate = safe_name
    counter = 2
    lowered = {name.lower() for name in used_names}
    while candidate.lower() in lowered:
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    used_names.add(candidate)
    return candidate


def unique_path(directory: Path, filename: Any, used_names: set[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = directory / dedupe_filename(filename, used_names)
        if not candidate.exists():
            return candidate
        filename = candidate.name


def parse_link_header(header: str | None) -> dict[str, str]:
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


def absolute_url(raw_url: str, domain: str) -> str:
    value = (raw_url or "").strip()
    if not value or value.startswith("#") or value.startswith("mailto:") or value.startswith("tel:"):
        return value
    return urljoin(f"{domain}/", value)


def extract_html_resources(html: str, domain: str = "", where_found: str = "page") -> list[ResourceRef]:
    soup = BeautifulSoup(html or "", "html.parser")
    resources: list[ResourceRef] = []
    for tag_name, attr in RESOURCE_ATTRS.items():
        for tag in soup.find_all(tag_name):
            value = tag.get(attr)
            if not value:
                continue
            resources.append(
                ResourceRef(
                    tag_name=tag_name,
                    attr=attr,
                    url=str(value),
                    absolute_url=absolute_url(str(value), domain) if domain else str(value),
                    link_text=tag.get_text(" ", strip=True),
                    where_found=where_found,
                )
            )
    return resources


def is_static_external_url(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in SAFE_EXTERNAL_EXTENSIONS


def classification_key(classification: ResourceClassification) -> str | None:
    if classification.kind == "canvas_file" and classification.id_or_slug:
        return f"file:{classification.id_or_slug}"
    if classification.kind == "canvas_page" and classification.id_or_slug:
        return f"page:{classification.id_or_slug}"
    if classification.kind == "canvas_assignment" and classification.id_or_slug:
        return f"assignment:{classification.id_or_slug}"
    if classification.kind == "canvas_discussion" and classification.id_or_slug:
        return f"discussion:{classification.id_or_slug}"
    if classification.kind == "canvas_module_item" and classification.id_or_slug:
        return f"module_item:{classification.id_or_slug}"
    if classification.kind == "canvas_quiz" and classification.id_or_slug:
        return f"quiz:{classification.id_or_slug}"
    return None


def normalize_canvas_url(url: str, domain: str, course_id: str) -> ResourceClassification:
    """Classify a resource URL found inside Canvas page HTML."""
    canvas_domain = normalize_domain(domain)
    absolute = absolute_url(url, canvas_domain)
    parsed = urlparse(absolute)
    canvas_host = urlparse(canvas_domain).netloc.lower()
    host = parsed.netloc.lower()
    path = unquote(parsed.path)

    if not parsed.scheme or absolute.startswith(("#", "mailto:", "tel:")):
        return ResourceClassification(
            kind="ignored",
            id_or_slug=None,
            downloadable_by_api=False,
            should_record_only=True,
            reason="Anchor, mail, telephone, or unsupported URL scheme.",
            normalized_url=absolute,
        )

    if host != canvas_host:
        reason = "External URL; not downloaded unless --download-external true and directly static."
        if any(keyword in host or keyword in path.lower() for keyword in PROTECTED_HOST_KEYWORDS):
            reason = "External/protected resource; recorded only."
        return ResourceClassification(
            kind="external_protected" if reason.startswith("External/protected") else "external",
            id_or_slug=None,
            downloadable_by_api=False,
            should_record_only=True,
            reason=reason,
            normalized_url=absolute,
        )

    file_patterns = [
        rf"/api/v1/courses/{re.escape(str(course_id))}/files/(\d+)(?:/|$)",
        rf"/courses/{re.escape(str(course_id))}/files/(\d+)(?:/|$)",
        r"/api/v1/files/(\d+)(?:/|$)",
        r"/files/(\d+)(?:/|$)",
    ]
    for pattern in file_patterns:
        match = re.search(pattern, path)
        if match:
            file_id = match.group(1)
            return ResourceClassification(
                kind="canvas_file",
                id_or_slug=file_id,
                downloadable_by_api=True,
                should_record_only=False,
                reason="Canvas-hosted file; downloadable through Files API.",
                normalized_url=f"{canvas_domain}/api/v1/files/{file_id}",
            )

    page_patterns = [
        rf"/api/v1/courses/{re.escape(str(course_id))}/pages/([^/?#]+)",
        rf"/courses/{re.escape(str(course_id))}/pages/([^/?#]+)",
    ]
    for pattern in page_patterns:
        match = re.search(pattern, path)
        if match:
            slug = unquote(match.group(1).rstrip("/"))
            return ResourceClassification(
                kind="canvas_page",
                id_or_slug=slug,
                downloadable_by_api=False,
                should_record_only=True,
                reason="Canvas page link.",
                normalized_url=f"{canvas_domain}/courses/{course_id}/pages/{slug}",
            )

    assignment_match = re.search(
        rf"/courses/{re.escape(str(course_id))}/assignments/(\d+)(?:/|$)", path
    )
    if assignment_match:
        assignment_id = assignment_match.group(1)
        return ResourceClassification(
            kind="canvas_assignment",
            id_or_slug=assignment_id,
            downloadable_by_api=False,
            should_record_only=True,
            reason="Canvas assignment link.",
            normalized_url=f"{canvas_domain}/courses/{course_id}/assignments/{assignment_id}",
        )

    module_item_match = re.search(
        rf"/courses/{re.escape(str(course_id))}/modules/items/(\d+)(?:/|$)", path
    )
    if module_item_match:
        item_id = module_item_match.group(1)
        return ResourceClassification(
            kind="canvas_module_item",
            id_or_slug=item_id,
            downloadable_by_api=False,
            should_record_only=True,
            reason="Canvas module item link.",
            normalized_url=f"{canvas_domain}/courses/{course_id}/modules/items/{item_id}",
        )

    module_match = re.search(rf"/courses/{re.escape(str(course_id))}/modules(?:/|$)", path)
    if module_match:
        return ResourceClassification(
            kind="canvas_module",
            id_or_slug=None,
            downloadable_by_api=False,
            should_record_only=True,
            reason="Canvas modules link.",
            normalized_url=f"{canvas_domain}/courses/{course_id}/modules",
        )

    discussion_match = re.search(
        rf"/courses/{re.escape(str(course_id))}/discussion_topics/(\d+)(?:/|$)", path
    )
    if discussion_match:
        topic_id = discussion_match.group(1)
        return ResourceClassification(
            kind="canvas_discussion",
            id_or_slug=topic_id,
            downloadable_by_api=False,
            should_record_only=True,
            reason="Canvas discussion topic link.",
            normalized_url=f"{canvas_domain}/courses/{course_id}/discussion_topics/{topic_id}",
        )

    quiz_match = re.search(rf"/courses/{re.escape(str(course_id))}/quizzes/(\d+)(?:/|$)", path)
    if quiz_match:
        quiz_id = quiz_match.group(1)
        return ResourceClassification(
            kind="canvas_quiz",
            id_or_slug=quiz_id,
            downloadable_by_api=False,
            should_record_only=True,
            reason="Canvas quiz link; metadata is recorded, but quizzes are not archived in Phase 2.",
            normalized_url=f"{canvas_domain}/courses/{course_id}/quizzes/{quiz_id}",
        )

    if "/external_tools/" in path or "/api/v1/external_tools/" in path:
        return ResourceClassification(
            kind="external_protected",
            id_or_slug=None,
            downloadable_by_api=False,
            should_record_only=True,
            reason="Canvas external tool/LTI link; recorded only.",
            normalized_url=absolute,
        )

    return ResourceClassification(
        kind="canvas_other",
        id_or_slug=None,
        downloadable_by_api=False,
        should_record_only=True,
            reason="Canvas URL is not a recognized downloadable file for this archive mode.",
        normalized_url=absolute,
    )


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


def html_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{
      margin: 0;
      background: #f7f8fb;
      color: #1f2937;
      font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      max-width: 960px;
      margin: 0 auto;
      padding: 28px 18px 48px;
    }}
    h1, h2 {{
      line-height: 1.2;
    }}
    a {{
      color: #186a8a;
      overflow-wrap: anywhere;
    }}
    .panel {{
      background: #fff;
      border: 1px solid #d8dee8;
      border-radius: 8px;
      padding: 16px;
      margin: 16px 0;
    }}
    .muted {{
      color: #5f6b7a;
    }}
    .external-link::after {{
      content: " external";
      color: #6b7280;
      font-size: 0.8em;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
    }}
    th, td {{
      border-bottom: 1px solid #d8dee8;
      padding: 8px;
      text-align: left;
      vertical-align: top;
    }}
    img, iframe, video {{
      max-width: 100%;
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


class CanvasClient:
    """Small Canvas REST API client."""

    def __init__(self, domain: str, token: str, verbose: bool = False, max_retries: int = 3) -> None:
        self.domain = normalize_domain(domain)
        self.base_url = f"{self.domain}/api/v1"
        self.verbose = verbose
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "CanvasCourseArchiver/1.0",
            }
        )

    def api_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
        return f"{self.base_url}{path}"

    def request(self, path_or_url: str, params: Any = None, stream: bool = False) -> requests.Response:
        url = self.api_url(path_or_url)
        for attempt in range(self.max_retries + 1):
            if self.verbose:
                print(f"GET {url}", file=sys.stderr)
            try:
                response = self.session.get(url, params=params, timeout=30, stream=stream)
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise CanvasAPIError(f"Could not connect to Canvas: {exc}") from exc

            if response.status_code in TRANSIENT_STATUS_CODES and attempt < self.max_retries:
                delay = self.retry_delay(response, attempt)
                print(f"Canvas returned {response.status_code}; retrying in {delay:.0f}s.", file=sys.stderr)
                time.sleep(delay)
                continue

            if response.status_code == 401:
                raise CanvasPermissionError(
                    "Canvas rejected the API token (401 Unauthorized). Check that the token is correct, not expired, and not revoked.",
                    401,
                )
            if response.status_code == 403:
                raise CanvasPermissionError(
                    "Canvas denied access (403 Forbidden). Your token is valid, but this account may not have permission for this course or page.",
                    403,
                )
            if response.status_code == 404:
                raise CanvasAPIError(
                    "Canvas could not find that course, page, or file (404 Not Found). Check the URL and your enrollment access.",
                    404,
                )
            if response.status_code >= 400:
                detail = response.text[:500].strip()
                raise CanvasAPIError(f"Canvas request failed with HTTP {response.status_code}: {detail}", response.status_code)

            return response

        raise CanvasAPIError(f"Canvas request failed after retries: {url}")

    def get_json(self, path_or_url: str, params: Any = None) -> Any:
        response = self.request(path_or_url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise CanvasAPIError(f"Canvas returned non-JSON content for {response.url}") from exc

    def get_paginated(self, path: str, params: Any = None) -> list[Any]:
        url = self.api_url(path)
        first = True
        results: list[Any] = []
        while url:
            response = self.request(url, params=params if first else None)
            first = False
            payload = response.json()
            if isinstance(payload, list):
                results.extend(payload)
            else:
                results.append(payload)
            url = parse_link_header(response.headers.get("Link")).get("next")
        return results

    def download_file(self, url: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        response = self.request(url, stream=True)
        total = int(response.headers.get("content-length") or 0)
        with local_path.open("wb") as handle:
            with tqdm(total=total or None, unit="B", unit_scale=True, desc=local_path.name[:40], leave=False) as bar:
                for chunk in response.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        handle.write(chunk)
                        bar.update(len(chunk))

    @staticmethod
    def retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                pass
        return float(min(30, 2**attempt))


class SinglePageArchiver:
    def __init__(
        self,
        client: CanvasClient,
        target: CoursePageTarget,
        output_dir: Path,
        download_external: bool = False,
        dry_run: bool = False,
        overwrite: bool = False,
        verbose: bool = False,
    ) -> None:
        self.client = client
        self.target = target
        self.output_dir = output_dir
        self.download_external = download_external
        self.dry_run = dry_run
        self.overwrite = overwrite
        self.verbose = verbose
        self.archive_timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self.archive_log: list[dict[str, Any]] = []
        self.external_links: list[dict[str, Any]] = []
        self.failed_downloads: list[dict[str, Any]] = []
        self.link_graph: dict[str, Any] = {"source_page": target.original_url, "outgoing_links": []}
        self.used_linked_names: set[str] = set()
        self.rewrite_map: dict[str, str] = {}

    def archive(self) -> int:
        course = self.fetch_course()
        page = self.fetch_page()
        resources = extract_html_resources(page.get("body") or "", self.target.domain, self.target.page_slug)
        classified = self.classify_resources(resources)

        if self.dry_run:
            self.print_dry_run_summary(course, page, classified)
            return 0

        course_dir = self.prepare_course_dir(course)
        page_path = course_dir / "pages" / f"{sanitize_filename(self.target.page_slug)}.html"
        linked_files_dir = course_dir / "linked_files" / "pages"

        for entry in classified:
            classification = entry["classification"]
            resource = entry["resource"]
            if classification.kind == "canvas_file":
                local_path = self.download_canvas_file(classification, linked_files_dir, resource)
                if local_path:
                    rel = rel_link(page_path.parent, local_path)
                    self.rewrite_map[resource.url] = rel
                    self.rewrite_map[resource.absolute_url] = rel
                    self.rewrite_map[classification.normalized_url] = rel
                    entry["local_path"] = str(local_path)
                    entry["status"] = "downloaded"
                else:
                    entry["status"] = "failed"
            elif classification.kind == "external" and self.download_external and is_static_external_url(resource.absolute_url):
                local_path = self.download_external_file(resource, linked_files_dir)
                if local_path:
                    rel = rel_link(page_path.parent, local_path)
                    self.rewrite_map[resource.url] = rel
                    self.rewrite_map[resource.absolute_url] = rel
                    entry["local_path"] = str(local_path)
                    entry["status"] = "downloaded_external"
                else:
                    self.record_external(resource, classification)
                    entry["status"] = "recorded"
            else:
                if classification.kind in {"external", "external_protected"}:
                    self.record_external(resource, classification)
                else:
                    self.update_graph(resource, None, "recorded")
                entry["status"] = "recorded"

        rewritten_body = rewrite_local_links(page.get("body") or "", self.target.domain, self.rewrite_map, classified)
        self.write_page(page_path, page, rewritten_body)
        write_json(course_dir / "course.json", self.course_summary(course))
        self.write_index(course_dir, course, page, page_path)
        self.write_reports(course_dir, bool(page_path.exists()), classified)
        print(f"Archive complete: {course_dir / 'index.html'}")
        return 0

    def fetch_course(self) -> dict[str, Any]:
        params = [("include[]", "syllabus_body")]
        try:
            course = self.client.get_json(f"/courses/{self.target.course_id}", params=params)
        except (CanvasAPIError, CanvasPermissionError) as exc:
            # 400 often means include[] is not supported; 403/404 means metadata is restricted
            status_code = getattr(exc, "status_code", None)
            if status_code == 400:
                course = self.client.get_json(f"/courses/{self.target.course_id}")
            else:
                print(f"Warning: Could not fetch course metadata ({exc}). Using ID as fallback name.", file=sys.stderr)
                return {"id": self.target.course_id, "name": f"Course {self.target.course_id}"}
        
        self.log("course", self.client.api_url(f"/courses/{self.target.course_id}"), None, "fetched")
        if not isinstance(course, dict):
            return {"id": self.target.course_id, "name": f"Course {self.target.course_id}"}
        return course

    def fetch_page(self) -> dict[str, Any]:
        encoded_slug = quote(self.target.page_slug, safe="")
        path = f"/courses/{self.target.course_id}/pages/{encoded_slug}"
        page = self.client.get_json(path)
        self.log("page", self.client.api_url(path), None, "fetched")
        if not isinstance(page, dict):
            raise CanvasAPIError("Canvas returned unexpected page metadata.")
        return page

    def prepare_course_dir(self, course: dict[str, Any]) -> Path:
        folder_name = self.course_folder_name(course)
        course_dir = self.output_dir / folder_name
        if course_dir.exists() and self.overwrite:
            shutil.rmtree(course_dir)
        course_dir.mkdir(parents=True, exist_ok=True)
        (course_dir / "pages").mkdir(exist_ok=True)
        (course_dir / "linked_files" / "pages").mkdir(parents=True, exist_ok=True)
        return course_dir

    def course_folder_name(self, course: dict[str, Any]) -> str:
        name = course.get("name") or course.get("course_code") or "Canvas Course"
        return f"{sanitize_filename(name, 80)} - {self.target.course_id}"

    def course_summary(self, course: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": course.get("id"),
            "name": course.get("name"),
            "course_code": course.get("course_code"),
            "workflow_state": course.get("workflow_state"),
            "default_view": course.get("default_view"),
            "original_canvas_course_url": f"{self.target.domain}/courses/{self.target.course_id}",
            "archive_timestamp": self.archive_timestamp,
        }

    def classify_resources(self, resources: list[ResourceRef]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for resource in resources:
            classification = normalize_canvas_url(resource.absolute_url, self.target.domain, self.target.course_id)
            entry = {
                "resource": resource,
                "classification": classification,
                "status": "pending",
                "local_path": None,
            }
            entries.append(entry)
            self.link_graph["outgoing_links"].append(
                {
                    "url": resource.absolute_url,
                    "raw_url": resource.url,
                    "link_text": resource.link_text,
                    "where_found": resource.where_found,
                    "classification": asdict(classification),
                    "local_path": None,
                    "status": "pending",
                }
            )
        return entries

    def download_canvas_file(
        self,
        classification: ResourceClassification,
        linked_files_dir: Path,
        resource: ResourceRef,
    ) -> Path | None:
        file_id = classification.id_or_slug
        if not file_id:
            return None
        try:
            metadata = self.client.get_json(f"/files/{file_id}")
            filename = (
                metadata.get("filename")
                or metadata.get("display_name")
                or metadata.get("name")
                or f"canvas-file-{file_id}"
            )
            download_url = metadata.get("url") or metadata.get("download_url")
            if not download_url:
                raise CanvasAPIError("Canvas file metadata did not include a download URL.")
            local_path = unique_path(linked_files_dir, filename, self.used_linked_names)
            self.client.download_file(download_url, local_path)
            self.log("canvas_file", classification.normalized_url, local_path, "downloaded")
            self.update_graph(resource, str(local_path), "downloaded")
            return local_path
        except Exception as exc:
            message = f"Could not download Canvas file {file_id}: {exc}"
            self.failed_downloads.append(
                {
                    "url": resource.absolute_url,
                    "file_id": file_id,
                    "error": str(exc),
                    "classification": asdict(classification),
                }
            )
            self.log("canvas_file", resource.absolute_url, None, "failed", str(exc))
            self.update_graph(resource, None, "failed")
            print(f"Warning: {message}", file=sys.stderr)
            return None

    def download_external_file(self, resource: ResourceRef, linked_files_dir: Path) -> Path | None:
        try:
            parsed = urlparse(resource.absolute_url)
            filename = Path(parsed.path).name or "external-file"
            local_path = unique_path(linked_files_dir, filename, self.used_linked_names)
            with requests.get(resource.absolute_url, timeout=30, stream=True, allow_redirects=True) as response:
                if response.status_code >= 400:
                    raise CanvasAPIError(f"HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";")[0].lower()
                if content_type in {"text/html", "application/javascript", "text/javascript"}:
                    raise CanvasAPIError(f"Refusing to download web page content-type {content_type}")
                total = int(response.headers.get("content-length") or 0)
                if total > MAX_EXTERNAL_BYTES:
                    raise CanvasAPIError("External file is too large for this archive mode.")
                written = 0
                with local_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 128):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_EXTERNAL_BYTES:
                            raise CanvasAPIError("External file exceeded this archive mode's size limit.")
                        handle.write(chunk)
            self.log("external_static_file", resource.absolute_url, local_path, "downloaded")
            self.update_graph(resource, str(local_path), "downloaded_external")
            return local_path
        except Exception as exc:
            self.failed_downloads.append({"url": resource.absolute_url, "error": str(exc)})
            self.log("external_static_file", resource.absolute_url, None, "failed", str(exc))
            self.update_graph(resource, None, "failed")
            return None

    def record_external(self, resource: ResourceRef, classification: ResourceClassification) -> None:
        self.external_links.append(
            {
                "url": resource.absolute_url,
                "link_text": resource.link_text,
                "where_found": resource.where_found,
                "reason_not_downloaded": classification.reason,
                "classification": asdict(classification),
            }
        )
        self.update_graph(resource, None, "recorded")

    def write_page(self, page_path: Path, page: dict[str, Any], body_html: str) -> None:
        title = page.get("title") or self.target.page_slug
        source = page.get("html_url") or self.target.original_url
        body = f"""
<p><a href="../index.html">Back to archive index</a></p>
<h1>{escape(title)}</h1>
<div class="panel muted">
  <div>Source Canvas URL: <a href="{escape(source)}">{escape(source)}</a></div>
  <div>Archived: {escape(self.archive_timestamp)}</div>
</div>
<article class="panel">
{body_html or '<p>No page body was returned by the Canvas API.</p>'}
</article>
"""
        write_text(page_path, html_shell(title, body))
        self.log("page", source, page_path, "saved")

    def write_index(self, course_dir: Path, course: dict[str, Any], page: dict[str, Any], page_path: Path) -> None:
        course_name = course.get("name") or "Canvas Course"
        page_title = page.get("title") or self.target.page_slug
        body = f"""
<h1>{escape(course_name)}</h1>
<div class="panel">
  <table>
    <tr><th>Course ID</th><td>{escape(self.target.course_id)}</td></tr>
    <tr><th>Page</th><td><a href="{escape(rel_link(course_dir, page_path))}">{escape(page_title)}</a></td></tr>
    <tr><th>Canvas page URL</th><td><a href="{escape(self.target.original_url)}">{escape(self.target.original_url)}</a></td></tr>
    <tr><th>Archived</th><td>{escape(self.archive_timestamp)}</td></tr>
    <tr><th>Canvas files downloaded</th><td>{self.coverage_counts()['canvas_files_downloaded']}</td></tr>
    <tr><th>External/protected links recorded</th><td>{len(self.external_links)}</td></tr>
    <tr><th>Failed downloads</th><td>{len(self.failed_downloads)}</td></tr>
  </table>
</div>
<div class="panel">
  <h2>Reports</h2>
  <ul>
    <li><a href="coverage_report.html">Coverage report</a></li>
    <li><a href="external_links.json">External links JSON</a></li>
    <li><a href="failed_downloads.json">Failed downloads JSON</a></li>
    <li><a href="archive_log.json">Archive log JSON</a></li>
    <li><a href="link_graph.json">Link graph JSON</a></li>
  </ul>
</div>
"""
        write_text(course_dir / "index.html", html_shell(course_name, body))

    def write_reports(self, course_dir: Path, page_saved: bool, classified: list[dict[str, Any]]) -> None:
        for graph_entry in self.link_graph["outgoing_links"]:
            for entry in classified:
                if graph_entry["raw_url"] == entry["resource"].url:
                    graph_entry["local_path"] = entry.get("local_path")
                    graph_entry["status"] = entry.get("status")
                    break

        coverage = self.coverage_counts()
        coverage.update(
            {
                "page_saved": page_saved,
                "source_page": self.target.original_url,
                "archive_timestamp": self.archive_timestamp,
            }
        )
        write_json(course_dir / "archive_log.json", self.archive_log)
        write_json(course_dir / "external_links.json", self.external_links)
        write_json(course_dir / "failed_downloads.json", self.failed_downloads)
        write_json(course_dir / "coverage_report.json", coverage)
        write_json(course_dir / "link_graph.json", self.link_graph)
        rows = "".join(
            f"<tr><th>{escape(str(key).replace('_', ' ').title())}</th><td>{escape(str(value))}</td></tr>"
            for key, value in coverage.items()
        )
        write_text(
            course_dir / "coverage_report.html",
            html_shell("Coverage Report", f"<h1>Coverage Report</h1><div class=\"panel\"><table>{rows}</table></div>"),
        )

    def coverage_counts(self) -> dict[str, int]:
        discovered = self.link_graph.get("outgoing_links", [])
        canvas_files = [
            item for item in discovered if item.get("classification", {}).get("kind") == "canvas_file"
        ]
        return {
            "links_discovered": len(discovered),
            "canvas_files_discovered": len(canvas_files),
            "canvas_files_downloaded": sum(1 for item in canvas_files if item.get("status") == "downloaded"),
            "external_protected_links_recorded": len(self.external_links),
            "failed_downloads": len(self.failed_downloads),
        }

    def update_graph(self, resource: ResourceRef, local_path: str | None, status: str) -> None:
        for item in self.link_graph["outgoing_links"]:
            if item["raw_url"] == resource.url and item["url"] == resource.absolute_url:
                item["local_path"] = local_path
                item["status"] = status
                return

    def log(
        self,
        canvas_type: str,
        original_url: str | None,
        local_path: Path | None,
        status: str,
        error: str | None = None,
    ) -> None:
        record = {
            "canvas_type": canvas_type,
            "original_url": original_url,
            "local_path": str(local_path) if local_path else None,
            "status": status,
        }
        if error:
            record["error"] = error
        self.archive_log.append(record)

    def print_dry_run_summary(
        self,
        course: dict[str, Any],
        page: dict[str, Any],
        classified: list[dict[str, Any]],
    ) -> None:
        print(f"\n--- Dry Run (Single Page): {course.get('name', 'Course ' + self.target.course_id)} ---")
        print(f"Target Page: {page.get('title') or self.target.page_slug}")
        print(f"Canvas URL:  {self.target.original_url}")
        
        groups: dict[str, int] = {
            "canvas_file": 0,
            "canvas_page": 0,
            "canvas_assignment": 0,
            "canvas_discussion": 0,
            "external/protected": 0,
            "other": 0
        }
        
        for item in classified:
            kind = item["classification"].kind
            if kind in groups:
                groups[kind] += 1
            elif kind in ("external", "external_protected"):
                groups["external/protected"] += 1
            else:
                groups["other"] += 1

        print("\nLinks discovered on this page:")
        for label, count in groups.items():
            if count > 0:
                print(f"- {label.replace('_', ' ').title():18}: {count}")

        if self.verbose:
            print("\nDetailed Link List:")
            for item in classified:
                print(f"  [{item['classification'].kind}] {item['resource'].absolute_url}")

        print("\nDry run complete. No files were downloaded or modified.")


def rewrite_local_links(
    html: str,
    domain: str,
    rewrite_map: dict[str, str],
    classified_entries: list[dict[str, Any]],
) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    classification_by_url = {
        entry["resource"].url: entry["classification"] for entry in classified_entries
    }
    classification_by_absolute = {
        entry["resource"].absolute_url: entry["classification"] for entry in classified_entries
    }
    for tag_name, attr in RESOURCE_ATTRS.items():
        for tag in soup.find_all(tag_name):
            raw = tag.get(attr)
            if not raw:
                continue
            abs_value = absolute_url(str(raw), domain)
            replacement = rewrite_map.get(str(raw)) or rewrite_map.get(abs_value)
            if replacement:
                tag[attr] = replacement
                continue
            classification = classification_by_url.get(str(raw)) or classification_by_absolute.get(abs_value)
            if (
                classification
                and classification.kind in {"external", "external_protected"}
                and tag_name == "a"
            ):
                existing_class = tag.get("class") or []
                if "external-link" not in existing_class:
                    tag["class"] = existing_class + ["external-link"]
                tag["target"] = "_blank"
                tag["rel"] = "noopener noreferrer"
    return str(soup)


def rewrite_known_local_links(
    html: str,
    domain: str,
    course_id: str,
    html_path: Path,
    resource_paths: dict[str, Path],
    url_paths: dict[str, Path] | None = None,
) -> str:
    """Rewrite Canvas links in a saved HTML file using known local path mappings."""
    soup = BeautifulSoup(html or "", "html.parser")
    url_paths = url_paths or {}
    for tag_name, attr in RESOURCE_ATTRS.items():
        for tag in soup.find_all(tag_name):
            if tag.get("data-archive-source") == "true":
                continue
            raw = tag.get(attr)
            if not raw:
                continue
            abs_value = absolute_url(str(raw), domain)
            classification = normalize_canvas_url(abs_value, domain, course_id)
            key = classification_key(classification)
            local_path = None
            if key:
                local_path = resource_paths.get(key)
            local_path = local_path or url_paths.get(str(raw)) or url_paths.get(abs_value)
            if local_path:
                tag[attr] = rel_link(html_path.parent, local_path)
            elif classification.kind in {"external", "external_protected"} and tag_name == "a":
                existing_class = tag.get("class") or []
                if "external-link" not in existing_class:
                    tag["class"] = existing_class + ["external-link"]
                tag["target"] = "_blank"
                tag["rel"] = "noopener noreferrer"
    return str(soup)


def render_module_index(module_name: str, item_records: list[dict[str, Any]], module_dir: Path) -> str:
    items = []
    for item in sorted(item_records, key=lambda record: record.get("position") or 0):
        title = item.get("title") or "Untitled"
        item_type = item.get("type") or "Unknown"
        local_path = item.get("local_path")
        external_url = item.get("external_url")
        canvas_url = item.get("html_url") or item.get("url")
        if local_path:
            label = f'<a href="{escape(rel_link(module_dir, Path(local_path)))}">{escape(str(title))}</a>'
        elif external_url:
            label = (
                f'<a class="external-link" target="_blank" rel="noopener noreferrer" '
                f'href="{escape(str(external_url))}">{escape(str(title))}</a>'
            )
        elif canvas_url:
            label = (
                f'<a data-archive-source="true" class="external-link" target="_blank" '
                f'rel="noopener noreferrer" href="{escape(str(canvas_url))}">{escape(str(title))}</a>'
            )
        else:
            label = escape(str(title))
        items.append(f"<li><strong>{escape(str(item_type))}</strong>: {label}</li>")
    body = f"""
<h1>{escape(module_name)}</h1>
<p><a href="../../index.html">Back to course index</a></p>
<div class="panel">
  <ol>
    {''.join(items) or '<li>No module items were returned by Canvas.</li>'}
  </ol>
</div>
"""
    return html_shell(module_name, body)


def coverage_report_html(coverage: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><th>{escape(str(key).replace('_', ' ').title())}</th><td>{escape(str(value))}</td></tr>"
        for key, value in coverage.items()
    )
    return html_shell("Coverage Report", f'<h1>Coverage Report</h1><div class="panel"><table>{rows}</table></div>')


class FullCourseArchiver:
    """Archives API-accessible content for one Canvas course."""

    def __init__(
        self,
        client: CanvasClient,
        target: CourseTarget,
        output_dir: Path,
        page_target: CoursePageTarget | None = None,
        download_external: bool = False,
        include_submissions: bool = False,
        dry_run: bool = False,
        overwrite: bool = False,
        crawl_depth: int = 2,
        verbose: bool = False,
    ) -> None:
        self.client = client
        self.target = target
        self.page_target = page_target
        self.output_dir = output_dir
        self.download_external = download_external
        self.include_submissions = include_submissions
        self.dry_run = dry_run
        self.overwrite = overwrite
        self.crawl_depth = max(0, crawl_depth)
        self.verbose = verbose
        self.archive_timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self.course_dir = output_dir / f"Canvas Course - {target.course_id}"

        self.archive_log: list[dict[str, Any]] = []
        self.archive_warnings: list[str] = []
        self.external_links: list[dict[str, Any]] = []
        self.failed_downloads: list[dict[str, Any]] = []
        self.link_graph: dict[str, Any] = {"source_course": target.original_url, "outgoing_links": []}
        self.coverage: dict[str, Any] = {
            "files_discovered": 0,
            "files_downloaded": 0,
            "files_failed": 0,
            "pages_discovered": 0,
            "pages_saved": 0,
            "pages_failed": 0,
            "assignments_discovered": 0,
            "assignments_saved": 0,
            "assignments_failed": 0,
            "modules_discovered": 0,
            "module_items_processed": 0,
            "discussions_discovered": 0,
            "discussions_saved": 0,
            "discussions_failed": 0,
            "announcements_discovered": 0,
            "announcements_saved": 0,
            "announcements_failed": 0,
            "syllabus_saved": False,
            "submissions_attempted": 0,
            "submissions_downloaded": 0,
            "submissions_failed": 0,
            "canvas_internal_links_discovered": 0,
            "canvas_internal_links_resolved": 0,
            "external_links_recorded": 0,
            "unsupported_resources": 0,
            "failed_downloads": 0,
        }

        self.used_names_by_dir: dict[str, set[str]] = {}
        self.resource_paths: dict[str, Path] = {}
        self.url_paths: dict[str, Path] = {}
        self.saved_html_files: list[Path] = []
        self.processed_keys: set[str] = set()

        self.files_index: list[dict[str, Any]] = []
        self.pages_index: list[dict[str, Any]] = []
        self.assignments_index: list[dict[str, Any]] = []
        self.modules_index: list[dict[str, Any]] = []
        self.discussion_index: list[dict[str, Any]] = []
        self.announcements_index: list[dict[str, Any]] = []
        self.submissions_index: list[dict[str, Any]] = []

    def archive(self) -> int:
        course = self.fetch_course()
        if self.dry_run:
            self.print_dry_run_summary(course)
            return 0

        self.prepare_course_dir(course)
        self.save_course_metadata(course)
        self.archive_course_files()
        self.archive_syllabus(course)
        
        # Seed crawling: if a specific page was supplied, fetch it and its children
        if self.page_target:
            self.save_page_by_slug(self.page_target.page_slug, depth=self.crawl_depth)
            
        # Seed crawling: if the default view is wiki, try the front page
        if course.get("default_view") == "wiki":
            self.archive_front_page()

        self.archive_pages()
        self.archive_assignments()
        self.archive_discussions(announcements=False)
        self.archive_discussions(announcements=True)
        self.archive_modules()
        if self.include_submissions:
            self.archive_submissions()

        self.second_pass_rewrite()
        self.write_main_index(course)
        self.write_audit_files()
        print(f"Archive complete: {self.course_dir / 'index.html'}")
        return 0

    def fetch_course(self) -> dict[str, Any]:
        try:
            course = self.client.get_json(
                f"/courses/{self.target.course_id}",
                params=[("include[]", "syllabus_body")],
            )
        except (CanvasAPIError, CanvasPermissionError) as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code == 400:
                self.warn("Canvas did not accept include[]=syllabus_body; fetching metadata without it.")
                course = self.client.get_json(f"/courses/{self.target.course_id}")
            else:
                self.warn(f"Could not fetch full course metadata ({exc}). Continuing with basic info.")
                return {"id": self.target.course_id, "name": f"Course {self.target.course_id}"}
        if not isinstance(course, dict):
            return {"id": self.target.course_id, "name": f"Course {self.target.course_id}"}
        return course

    def print_dry_run_summary(self, course: dict[str, Any]) -> None:
        print(f"\n--- Dry Run: {course.get('name', 'Course ' + self.target.course_id)} ---")
        print(f"Canvas Domain: {self.target.domain}")
        print(f"Default View:  {course.get('default_view')}")
        print(f"Syllabus Body: {'Returned' if (course.get('syllabus_body') or '').strip() else 'Empty or not returned'}")
        
        # Check top-level endpoints
        endpoints = [
            ("Files", f"/courses/{self.target.course_id}/files"),
            ("Pages", f"/courses/{self.target.course_id}/pages"),
            ("Assignments", f"/courses/{self.target.course_id}/assignments"),
            ("Modules", f"/courses/{self.target.course_id}/modules"),
            ("Discussions", f"/courses/{self.target.course_id}/discussion_topics"),
            ("Announcements", f"/courses/{self.target.course_id}/discussion_topics?only_announcements=true"),
        ]
        
        collection_data = {}
        print("\nChecking Top-Level Endpoints:")
        for label, path in endpoints:
            items, status = self.fetch_collection_status(path, label)
            collection_data[label] = items
            print(f"- {label:13}: {status}")

        # If wiki view, check front page
        front_page_body = ""
        if course.get("default_view") == "wiki":
            try:
                fp = self.client.get_json(f"/courses/{self.target.course_id}/front_page")
                front_page_body = fp.get("body") or ""
                print(f"- {'Front Page':13}: Accessible ('{fp.get('title')}')")
            except Exception as exc:
                print(f"- {'Front Page':13}: Not accessible ({exc})")

        # Discover links from Syllabus and Front Page
        seeds = []
        if (course.get("syllabus_body") or "").strip():
            seeds.append(("Syllabus", course["syllabus_body"]))
        if front_page_body.strip():
            seeds.append(("Front Page", front_page_body))
        if self.page_target:
            try:
                target_page = self.client.get_json(f"/courses/{self.target.course_id}/pages/{quote(self.page_target.page_slug, safe='')}")
                if target_page.get("body"):
                    seeds.append((f"Seed Page ({self.page_target.page_slug})", target_page["body"]))
                    print(f"- {'Seed Page':13}: Accessible ('{target_page.get('title')}')")
            except Exception as exc:
                print(f"- {'Seed Page':13}: Not accessible ({exc})")

        if seeds:
            print("\nDiscovering links from crawl seeds (Syllabus, Front Page, provided Page):")
            for label, body in seeds:
                self.print_discovered_links(body, label)

        print("\nDry run complete. No files were downloaded or modified.")

    def fetch_collection_status(self, path: str, label: str) -> tuple[list[Any], str]:
        try:
            items = self.client.get_paginated(path, params={"per_page": 100})
            if not items:
                return [], "Accessible (0 items found)"
            return items, f"Accessible ({len(items)} items found)"
        except CanvasPermissionError as exc:
            if exc.status_code == 403:
                return [], "Permission denied (403 Forbidden)"
            return [], f"Inaccessible ({exc})"
        except CanvasAPIError as exc:
            if exc.status_code == 404:
                return [], "Not found / Not enabled (404 Not Found)"
            return [], f"Inaccessible ({exc})"
        except Exception as exc:
            return [], f"Inaccessible ({exc})"

    def print_discovered_links(self, html: str, source_label: str) -> None:
        resources = extract_html_resources(html, self.target.domain, source_label)
        groups: dict[str, list[ResourceRef]] = {
            "Pages": [],
            "Files": [],
            "Assignments": [],
            "Discussions": [],
            "External/Protected": [],
            "Other": []
        }
        for res in resources:
            cls = normalize_canvas_url(res.absolute_url, self.target.domain, self.target.course_id)
            if cls.kind == "canvas_page":
                groups["Pages"].append(res)
            elif cls.kind == "canvas_file":
                groups["Files"].append(res)
            elif cls.kind == "canvas_assignment":
                groups["Assignments"].append(res)
            elif cls.kind == "canvas_discussion":
                groups["Discussions"].append(res)
            elif cls.kind in ("external", "external_protected"):
                groups["External/Protected"].append(res)
            else:
                groups["Other"].append(res)

        print(f"  From {source_label}:")
        for group_name, res_list in groups.items():
            if res_list:
                # Deduplicate by absolute URL for summary
                unique_urls = {r.absolute_url for r in res_list}
                print(f"    - {group_name:18}: {len(unique_urls)} unique links found")
                if self.verbose:
                    for url in sorted(unique_urls):
                        print(f"        {url}")

    def prepare_course_dir(self, course: dict[str, Any]) -> None:
        self.course_dir = self.output_dir / self.course_folder_name(course)
        if self.course_dir.exists() and self.overwrite:
            shutil.rmtree(self.course_dir)
        for relative in [
            "",
            "syllabus",
            "modules",
            "pages",
            "assignments",
            "files/_unfiled",
            "linked_files/pages",
            "linked_files/assignments",
            "linked_files/syllabus",
            "linked_files/discussions",
            "linked_files/announcements",
            "discussions",
            "announcements",
            "submissions",
        ]:
            (self.course_dir / relative).mkdir(parents=True, exist_ok=True)

    def course_folder_name(self, course: dict[str, Any]) -> str:
        name = course.get("name") or course.get("course_code") or "Canvas Course"
        return f"{sanitize_filename(name, 80)} - {self.target.course_id}"

    def save_course_metadata(self, course: dict[str, Any]) -> None:
        course_json = dict(course)
        course_json["original_canvas_course_url"] = self.target.original_url
        course_json["archive_timestamp"] = self.archive_timestamp
        write_json(self.course_dir / "course.json", course_json)
        rows = "".join(
            f"<tr><th>{escape(label)}</th><td>{value}</td></tr>"
            for label, value in [
                ("ID", escape(str(course.get("id") or self.target.course_id))),
                ("Name", escape(str(course.get("name") or ""))),
                ("Course Code", escape(str(course.get("course_code") or ""))),
                ("Workflow State", escape(str(course.get("workflow_state") or ""))),
                ("Default View", escape(str(course.get("default_view") or ""))),
                (
                    "Original Canvas Course URL",
                    f'<a data-archive-source="true" href="{escape(self.target.original_url)}">{escape(self.target.original_url)}</a>',
                ),
                ("Archived", escape(self.archive_timestamp)),
            ]
        )
        write_text(
            self.course_dir / "course_summary.html",
            html_shell("Course Summary", f"<h1>Course Summary</h1><div class=\"panel\"><table>{rows}</table></div>"),
        )
        self.saved_html_files.append(self.course_dir / "course_summary.html")
        self.log("course", self.client.api_url(f"/courses/{self.target.course_id}"), self.course_dir / "course.json", "saved")

    def archive_syllabus(self, course: dict[str, Any]) -> None:
        body = course.get("syllabus_body") or ""
        syllabus_dir = self.course_dir / "syllabus"
        if body.strip():
            path = syllabus_dir / "syllabus.html"
            rewritten = self.process_html_fragment(
                body,
                path,
                self.course_dir / "linked_files" / "syllabus",
                "syllabus",
                "Syllabus",
                self.crawl_depth,
            )
            self.write_wrapped_html(
                path,
                "Syllabus",
                f"{self.target.original_url}/assignments/syllabus",
                rewritten,
            )
            self.coverage["syllabus_saved"] = True
            self.resource_paths["syllabus"] = path
            self.log("syllabus", f"{self.target.original_url}/assignments/syllabus", path, "saved")
        else:
            readme = syllabus_dir / "README.txt"
            write_text(
                readme,
                "No syllabus body was returned by the Canvas API. The syllabus may be a Canvas Page, uploaded file, module item, or external link.\n",
            )
            self.coverage["syllabus_saved"] = False
            self.log("syllabus", self.client.api_url(f"/courses/{self.target.course_id}"), readme, "empty")

    def archive_course_files(self) -> None:
        files = self.safe_paginated(f"/courses/{self.target.course_id}/files", {"per_page": 100}, "files")
        self.coverage["files_discovered"] = len(files)
        for file_info in tqdm(files, desc="Course files"):
            self.save_course_file(file_info)
        write_json(self.course_dir / "files" / "files_index.json", self.files_index)

    def save_course_file(self, file_info: dict[str, Any]) -> Path | None:
        file_id = str(file_info.get("id") or "")
        key = f"file:{file_id}" if file_id else None
        if key and key in self.resource_paths:
            return self.resource_paths[key]
        filename = file_info.get("filename") or file_info.get("display_name") or file_info.get("name") or f"file-{file_id or 'unknown'}"
        dest_dir = self.course_file_dir(file_info)
        path = unique_path(dest_dir, filename, self.used_set(dest_dir))
        download_url = file_info.get("url") or file_info.get("download_url")
        record = self.file_index_record(file_info, path, "pending")
        try:
            if not download_url:
                raise CanvasAPIError("Canvas file metadata did not include a download URL.")
            self.client.download_file(download_url, path)
            record["status"] = "downloaded"
            self.coverage["files_downloaded"] += 1
            self.log("file", file_info.get("html_url") or download_url, path, "downloaded")
            self.map_file_paths(file_info, path)
            return path
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            self.coverage["files_failed"] += 1
            self.record_failed(file_info.get("html_url") or download_url, "file", str(exc))
            return None
        finally:
            self.files_index.append(record)

    def course_file_dir(self, file_info: dict[str, Any]) -> Path:
        folder = file_info.get("folder_path") or ""
        full_name = file_info.get("full_name") or ""
        if not folder and "/" in full_name:
            folder = str(Path(full_name).parent)
        if not folder or folder in {".", "/"}:
            return self.course_dir / "files" / "_unfiled"
        parts = [sanitize_filename(part, 80) for part in folder.split("/") if part and part != "course files"]
        return self.course_dir / "files" / Path(*parts) if parts else self.course_dir / "files" / "_unfiled"

    def file_index_record(self, file_info: dict[str, Any], path: Path, status: str) -> dict[str, Any]:
        file_id = file_info.get("id")
        return {
            "id": file_id,
            "display_name": file_info.get("display_name"),
            "filename": file_info.get("filename"),
            "content_type": file_info.get("content-type") or file_info.get("content_type"),
            "size": file_info.get("size"),
            "created_at": file_info.get("created_at"),
            "updated_at": file_info.get("updated_at") or file_info.get("modified_at"),
            "original_canvas_url": file_info.get("html_url") or file_info.get("url"),
            "api_url": self.client.api_url(f"/files/{file_id}") if file_id else None,
            "local_path": str(path),
            "status": status,
        }

    def archive_pages(self) -> None:
        pages = self.safe_paginated(f"/courses/{self.target.course_id}/pages", {"per_page": 100}, "pages")
        self.coverage["pages_discovered"] = len(pages)
        for index, page in enumerate(tqdm(pages, desc="Pages"), start=1):
            slug = page.get("url") or page.get("page_id") or page.get("id")
            if slug:
                self.save_page_by_slug(str(slug), index=index, depth=self.crawl_depth)
        write_json(self.course_dir / "pages" / "pages_index.json", self.pages_index)

    def save_page_by_slug(self, slug: str, index: int | None = None, depth: int = 0) -> Path | None:
        key = f"page:{slug}"
        if key in self.resource_paths:
            return self.resource_paths[key]
        if key in self.processed_keys:
            return None
        self.processed_keys.add(key)
        try:
            detail = self.client.get_json(f"/courses/{self.target.course_id}/pages/{quote(slug, safe='')}")
            title = detail.get("title") or slug
            prefix = f"{index:03d} - " if index else ""
            path = unique_path(self.course_dir / "pages", f"{prefix}{sanitize_filename(slug, 90)}.html", self.used_set(self.course_dir / "pages"))
            self.resource_paths[key] = path
            self.url_paths[f"{self.target.domain}/courses/{self.target.course_id}/pages/{slug}"] = path
            rewritten = self.process_html_fragment(
                detail.get("body") or "",
                path,
                self.course_dir / "linked_files" / "pages",
                "page",
                title,
                depth,
            )
            self.write_wrapped_html(path, title, detail.get("html_url") or f"{self.target.original_url}/pages/{slug}", rewritten)
            record = {"url": slug, "title": title, "page_id": detail.get("page_id"), "html_url": detail.get("html_url"), "local_path": str(path), "status": "saved"}
            self.pages_index.append(record)
            self.coverage["pages_saved"] += 1
            self.log("page", detail.get("html_url"), path, "saved")
            return path
        except Exception as exc:
            self.coverage["pages_failed"] += 1
            self.record_failed(f"{self.target.original_url}/pages/{slug}", "page", str(exc))
            return None

    def archive_assignments(self) -> None:
        assignments = self.safe_paginated(f"/courses/{self.target.course_id}/assignments", {"per_page": 100}, "assignments")
        self.coverage["assignments_discovered"] = len(assignments)
        for index, assignment in enumerate(tqdm(assignments, desc="Assignments"), start=1):
            assignment_id = assignment.get("id")
            if assignment_id:
                self.save_assignment(str(assignment_id), assignment, index=index, depth=self.crawl_depth)
        write_json(self.course_dir / "assignments" / "assignments_index.json", self.assignments_index)

    def save_assignment(self, assignment_id: str, assignment: dict[str, Any] | None = None, index: int | None = None, depth: int = 0) -> Path | None:
        key = f"assignment:{assignment_id}"
        if key in self.resource_paths:
            return self.resource_paths[key]
        if key in self.processed_keys:
            return None
        self.processed_keys.add(key)
        try:
            detail = assignment or self.client.get_json(f"/courses/{self.target.course_id}/assignments/{assignment_id}")
            if assignment is not None:
                try:
                    detail = self.client.get_json(f"/courses/{self.target.course_id}/assignments/{assignment_id}")
                except CanvasAPIError:
                    detail = assignment
            name = detail.get("name") or detail.get("title") or f"Assignment {assignment_id}"
            prefix = f"{index:03d} - " if index else ""
            path = unique_path(self.course_dir / "assignments", f"{prefix}{sanitize_filename(name, 90)}.html", self.used_set(self.course_dir / "assignments"))
            self.resource_paths[key] = path
            self.url_paths[f"{self.target.domain}/courses/{self.target.course_id}/assignments/{assignment_id}"] = path
            body = detail.get("description") or ""
            rewritten = self.process_html_fragment(
                body,
                path,
                self.course_dir / "linked_files" / "assignments",
                "assignment",
                name,
                depth,
            )
            meta = {
                "Due": detail.get("due_at"),
                "Points": detail.get("points_possible"),
                "Submission Types": ", ".join(detail.get("submission_types") or []),
            }
            self.write_wrapped_html(path, name, detail.get("html_url") or f"{self.target.original_url}/assignments/{assignment_id}", rewritten, meta)
            record = {"id": assignment_id, "name": name, "local_path": str(path), "html_url": detail.get("html_url"), "due_at": detail.get("due_at"), "points_possible": detail.get("points_possible"), "status": "saved"}
            self.assignments_index.append(record)
            self.coverage["assignments_saved"] += 1
            self.log("assignment", detail.get("html_url"), path, "saved")
            return path
        except Exception as exc:
            self.coverage["assignments_failed"] += 1
            self.record_failed(f"{self.target.original_url}/assignments/{assignment_id}", "assignment", str(exc))
            return None

    def archive_discussions(self, announcements: bool) -> None:
        label = "announcements" if announcements else "discussions"
        out_dir = self.course_dir / label
        linked_dir = self.course_dir / "linked_files" / label
        params = {"per_page": 100}
        if announcements:
            params["only_announcements"] = "true"
        topics = self.safe_paginated(f"/courses/{self.target.course_id}/discussion_topics", params, label)
        if announcements:
            self.coverage["announcements_discovered"] = len(topics)
        else:
            self.coverage["discussions_discovered"] = len(topics)

        index_records: list[dict[str, Any]] = []
        for index, topic in enumerate(tqdm(topics, desc=label.title()), start=1):
            topic_id = topic.get("id")
            if not topic_id:
                continue
            path = self.save_discussion(str(topic_id), topic, out_dir, linked_dir, index=index, announcements=announcements, depth=self.crawl_depth)
            if path:
                index_records.append({"id": topic_id, "title": topic.get("title"), "local_path": str(path), "html_url": topic.get("html_url")})

        if announcements:
            self.announcements_index = index_records
            write_json(out_dir / "announcements_index.json", self.announcements_index)
        else:
            self.discussion_index = index_records
            write_json(out_dir / "discussion_index.json", self.discussion_index)

    def save_discussion(
        self,
        topic_id: str,
        topic: dict[str, Any] | None,
        out_dir: Path,
        linked_dir: Path,
        index: int | None = None,
        announcements: bool = False,
        depth: int = 0,
    ) -> Path | None:
        key = f"discussion:{topic_id}"
        if key in self.resource_paths:
            return self.resource_paths[key]
        if key in self.processed_keys:
            return None
        self.processed_keys.add(key)
        try:
            detail = topic or self.client.get_json(f"/courses/{self.target.course_id}/discussion_topics/{topic_id}")
            if topic is not None:
                try:
                    detail = self.client.get_json(f"/courses/{self.target.course_id}/discussion_topics/{topic_id}")
                except CanvasAPIError:
                    detail = topic
            title = detail.get("title") or f"Discussion {topic_id}"
            prefix = f"{index:03d} - " if index else ""
            path = unique_path(out_dir, f"{prefix}{sanitize_filename(title, 90)}.html", self.used_set(out_dir))
            self.resource_paths[key] = path
            self.url_paths[f"{self.target.domain}/courses/{self.target.course_id}/discussion_topics/{topic_id}"] = path
            body = detail.get("message") or ""
            rewritten = self.process_html_fragment(
                body,
                path,
                linked_dir,
                "announcement" if announcements else "discussion",
                title,
                depth,
            )
            self.download_attachments(detail.get("attachments") or [], linked_dir, "announcement" if announcements else "discussion")
            self.write_wrapped_html(path, title, detail.get("html_url") or f"{self.target.original_url}/discussion_topics/{topic_id}", rewritten, {"Posted": detail.get("posted_at")})
            if announcements:
                self.coverage["announcements_saved"] += 1
            else:
                self.coverage["discussions_saved"] += 1
            self.log("announcement" if announcements else "discussion", detail.get("html_url"), path, "saved")
            return path
        except Exception as exc:
            if announcements:
                self.coverage["announcements_failed"] += 1
            else:
                self.coverage["discussions_failed"] += 1
            self.record_failed(f"{self.target.original_url}/discussion_topics/{topic_id}", "announcement" if announcements else "discussion", str(exc))
            return None

    def archive_modules(self) -> None:
        modules = self.safe_paginated(f"/courses/{self.target.course_id}/modules", {"per_page": 100}, "modules")
        self.coverage["modules_discovered"] = len(modules)
        for module_index, module in enumerate(tqdm(modules, desc="Modules"), start=1):
            module_id = module.get("id")
            module_name = module.get("name") or f"Module {module_index}"
            module_dir = unique_path(self.course_dir / "modules", f"{module_index:02d} - {sanitize_filename(module_name, 90)}", self.used_set(self.course_dir / "modules"))
            module_dir.mkdir(parents=True, exist_ok=True)
            items = []
            if module_id:
                items = self.safe_paginated(f"/courses/{self.target.course_id}/modules/{module_id}/items", {"per_page": 100}, f"module {module_id} items")
            item_records = [self.handle_module_item(item, module_dir) for item in items]
            self.coverage["module_items_processed"] += len(item_records)
            write_json(module_dir / "module_items.json", item_records)
            write_text(module_dir / "module_index.html", render_module_index(module_name, item_records, module_dir))
            self.saved_html_files.append(module_dir / "module_index.html")
            module_record = {"id": module_id, "name": module_name, "position": module.get("position") or module_index, "local_path": str(module_dir / "module_index.html"), "items": item_records}
            self.modules_index.append(module_record)
            self.log("module", self.client.api_url(f"/courses/{self.target.course_id}/modules/{module_id}"), module_dir / "module_index.html", "saved")
        write_json(self.course_dir / "modules" / "modules_index.json", self.modules_index)

    def archive_front_page(self) -> Path | None:
        try:
            front_page = self.client.get_json(f"/courses/{self.target.course_id}/front_page")
            slug = front_page.get("url")
            if slug:
                return self.save_page_by_slug(str(slug), depth=self.crawl_depth)
        except Exception as exc:
            self.warn(f"Could not fetch course front page: {exc}")
        return None

    def handle_module_item(self, item: dict[str, Any], module_dir: Path) -> dict[str, Any]:
        item_type = item.get("type") or "Unknown"
        title = item.get("title") or f"{item_type} {item.get('id') or ''}".strip()
        record = {
            "id": item.get("id"),
            "type": item_type,
            "title": title,
            "position": item.get("position"),
            "html_url": item.get("html_url"),
            "url": item.get("url"),
            "external_url": item.get("external_url"),
            "content_id": item.get("content_id"),
            "local_path": None,
            "status": "metadata_only",
        }
        try:
            if item_type == "Page" and item.get("page_url"):
                path = self.save_page_by_slug(str(item["page_url"]), depth=max(0, self.crawl_depth - 1))
                record["local_path"] = str(path) if path else None
                record["status"] = "saved" if path else "failed"
            elif item_type == "File" and item.get("content_id"):
                path = self.download_canvas_file_by_id(str(item["content_id"]), module_dir / "files", item.get("url") or item.get("html_url"))
                record["local_path"] = str(path) if path else None
                record["status"] = "downloaded" if path else "failed"
            elif item_type == "Assignment" and item.get("content_id"):
                path = self.save_assignment(str(item["content_id"]), depth=max(0, self.crawl_depth - 1))
                record["local_path"] = str(path) if path else None
                record["status"] = "saved" if path else "failed"
            elif item_type in {"Discussion", "DiscussionTopic"} and item.get("content_id"):
                path = self.save_discussion(str(item["content_id"]), None, self.course_dir / "discussions", self.course_dir / "linked_files" / "discussions", depth=max(0, self.crawl_depth - 1))
                record["local_path"] = str(path) if path else None
                record["status"] = "saved" if path else "failed"
            elif item_type == "ExternalUrl":
                if item.get("external_url"):
                    self.record_external_url(item["external_url"], "module_item", title, "External module URL; recorded only.")
                record["status"] = "recorded"
            elif item_type == "ExternalTool":
                self.record_external_url(item.get("html_url") or item.get("url") or "", "module_item", title, "Canvas ExternalTool/LTI item; recorded only.")
                record["status"] = "recorded"
            elif item_type == "Quiz":
                self.warn(f"Quiz module item '{title}' is recorded but not archived.")
                self.coverage["unsupported_resources"] += 1
                record["status"] = "unsupported"
            else:
                self.warn(f"Unsupported module item '{title}' of type '{item_type}' was recorded only.")
                self.coverage["unsupported_resources"] += 1
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            self.record_failed(item.get("html_url") or item.get("url"), "module_item", str(exc))
        return record

    def archive_submissions(self) -> None:
        submissions_dir = self.course_dir / "submissions"
        for assignment in tqdm(self.assignments_index, desc="Submissions"):
            assignment_id = assignment.get("id")
            if not assignment_id:
                continue
            self.coverage["submissions_attempted"] += 1
            api_path = f"/courses/{self.target.course_id}/assignments/{assignment_id}/submissions/self"
            try:
                submission = self.client.get_json(api_path, params=[("include[]", "submission_comments")])
                path = unique_path(submissions_dir, f"{sanitize_filename(assignment.get('name') or assignment_id, 90)} - submission.json", self.used_set(submissions_dir))
                write_json(path, submission)
                attachments_dir = submissions_dir / "attachments"
                downloaded = []
                for attachment in submission.get("attachments") or []:
                    local = self.download_attachment(attachment, attachments_dir, "submission")
                    if local:
                        downloaded.append(str(local))
                record = {"assignment_id": assignment_id, "local_path": str(path), "attachments": downloaded, "comments": submission.get("submission_comments")}
                self.submissions_index.append(record)
                self.coverage["submissions_downloaded"] += 1
                self.log("submission", self.client.api_url(api_path), path, "saved")
            except Exception as exc:
                self.coverage["submissions_failed"] += 1
                self.record_failed(self.client.api_url(api_path), "submission", str(exc))
        write_json(submissions_dir / "submissions_index.json", self.submissions_index)

    def process_html_fragment(
        self,
        html: str,
        html_path: Path,
        linked_dir: Path,
        source_type: str,
        source_title: str,
        depth: int,
    ) -> str:
        resources = extract_html_resources(html, self.target.domain, source_title)
        for resource in resources:
            classification = normalize_canvas_url(resource.absolute_url, self.target.domain, self.target.course_id)
            key = classification_key(classification)
            status = "recorded"
            local_path = None
            if classification.kind.startswith("canvas_") and classification.kind != "canvas_other":
                self.coverage["canvas_internal_links_discovered"] += 1
            if classification.kind == "canvas_file" and classification.id_or_slug:
                local_path = self.download_canvas_file_by_id(classification.id_or_slug, linked_dir, resource.absolute_url)
                status = "downloaded" if local_path else "failed"
            elif classification.kind == "external" and self.download_external and is_static_external_url(resource.absolute_url):
                local_path = self.download_external_file(resource, linked_dir)
                status = "downloaded_external" if local_path else "recorded"
                if not local_path:
                    self.record_external(resource, classification)
            elif classification.kind in {"external", "external_protected"}:
                self.record_external(resource, classification)
            elif depth > 0:
                local_path = self.resolve_internal_resource(classification, depth - 1)
                status = "resolved" if local_path else "recorded"
            elif classification.kind in {"canvas_quiz", "canvas_module", "canvas_module_item", "canvas_other"}:
                self.coverage["unsupported_resources"] += 1
            if local_path:
                self.coverage["canvas_internal_links_resolved"] += 1
            self.record_graph(resource, classification, source_type, source_title, local_path, status)
            if key and local_path:
                self.resource_paths[key] = local_path
        return rewrite_known_local_links(html, self.target.domain, self.target.course_id, html_path, self.resource_paths, self.url_paths)

    def resolve_internal_resource(self, classification: ResourceClassification, depth: int) -> Path | None:
        key = classification_key(classification)
        if key and key in self.resource_paths:
            return self.resource_paths[key]
        if classification.kind == "canvas_page" and classification.id_or_slug:
            return self.save_page_by_slug(classification.id_or_slug, depth=depth)
        if classification.kind == "canvas_assignment" and classification.id_or_slug:
            return self.save_assignment(classification.id_or_slug, depth=depth)
        if classification.kind == "canvas_discussion" and classification.id_or_slug:
            return self.save_discussion(classification.id_or_slug, None, self.course_dir / "discussions", self.course_dir / "linked_files" / "discussions", depth=depth)
        return None

    def download_canvas_file_by_id(self, file_id: str, dest_dir: Path, source_url: str | None = None) -> Path | None:
        key = f"file:{file_id}"
        if key in self.resource_paths:
            return self.resource_paths[key]
        try:
            metadata = self.client.get_json(f"/files/{file_id}")
            filename = metadata.get("filename") or metadata.get("display_name") or metadata.get("name") or f"canvas-file-{file_id}"
            download_url = metadata.get("url") or metadata.get("download_url")
            if not download_url:
                raise CanvasAPIError("Canvas file metadata did not include a download URL.")
            path = unique_path(dest_dir, filename, self.used_set(dest_dir))
            self.client.download_file(download_url, path)
            self.map_file_paths(metadata, path)
            self.coverage["files_downloaded"] += 1
            self.log("linked_file", source_url or self.client.api_url(f"/files/{file_id}"), path, "downloaded")
            return path
        except Exception as exc:
            self.coverage["files_failed"] += 1
            self.record_failed(source_url or self.client.api_url(f"/files/{file_id}"), "linked_file", str(exc))
            return None

    def download_external_file(self, resource: ResourceRef, dest_dir: Path) -> Path | None:
        if normalize_canvas_url(resource.absolute_url, self.target.domain, self.target.course_id).kind == "external_protected":
            self.record_external(resource, normalize_canvas_url(resource.absolute_url, self.target.domain, self.target.course_id))
            return None
        try:
            filename = Path(urlparse(resource.absolute_url).path).name or "external-file"
            path = unique_path(dest_dir, filename, self.used_set(dest_dir))
            with requests.get(resource.absolute_url, timeout=30, stream=True, allow_redirects=True) as response:
                if response.status_code >= 400:
                    raise CanvasAPIError(f"HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";")[0].lower()
                if content_type in {"text/html", "application/javascript", "text/javascript"}:
                    raise CanvasAPIError(f"Refusing to download web page content-type {content_type}")
                total = int(response.headers.get("content-length") or 0)
                if total > MAX_EXTERNAL_BYTES:
                    raise CanvasAPIError("External file is too large.")
                written = 0
                with path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 128):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_EXTERNAL_BYTES:
                            raise CanvasAPIError("External file exceeded size limit.")
                        handle.write(chunk)
            self.url_paths[resource.absolute_url] = path
            self.log("external_static_file", resource.absolute_url, path, "downloaded")
            return path
        except Exception as exc:
            self.record_failed(resource.absolute_url, "external_static_file", str(exc))
            return None

    def download_attachments(self, attachments: list[dict[str, Any]], dest_dir: Path, source_type: str) -> None:
        for attachment in attachments:
            self.download_attachment(attachment, dest_dir, source_type)

    def download_attachment(self, attachment: dict[str, Any], dest_dir: Path, source_type: str) -> Path | None:
        url = attachment.get("url")
        if attachment.get("id"):
            return self.download_canvas_file_by_id(str(attachment["id"]), dest_dir, url)
        if not url:
            return None
        filename = attachment.get("filename") or attachment.get("display_name") or "attachment"
        try:
            path = unique_path(dest_dir, filename, self.used_set(dest_dir))
            self.client.download_file(url, path)
            self.log(source_type, url, path, "downloaded_attachment")
            return path
        except Exception as exc:
            self.record_failed(url, source_type, str(exc))
            return None

    def map_file_paths(self, file_info: dict[str, Any], path: Path) -> None:
        file_id = file_info.get("id")
        if file_id:
            self.resource_paths[f"file:{file_id}"] = path
            self.url_paths[self.client.api_url(f"/files/{file_id}")] = path
            self.url_paths[f"{self.target.domain}/files/{file_id}/download"] = path
            self.url_paths[f"{self.target.domain}/courses/{self.target.course_id}/files/{file_id}"] = path
            self.url_paths[f"{self.target.domain}/courses/{self.target.course_id}/files/{file_id}/download"] = path
        for url_key in [file_info.get("url"), file_info.get("download_url"), file_info.get("html_url")]:
            if url_key:
                self.url_paths[str(url_key)] = path

    def write_wrapped_html(
        self,
        path: Path,
        title: str,
        source_url: str | None,
        body_html: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        rows = ""
        for key, value in (metadata or {}).items():
            if value not in (None, ""):
                rows += f"<tr><th>{escape(str(key))}</th><td>{escape(str(value))}</td></tr>"
        meta_table = f'<div class="panel"><table>{rows}</table></div>' if rows else ""
        source = ""
        if source_url:
            source = (
                f'<div>Source Canvas URL: <a data-archive-source="true" href="{escape(source_url)}">'
                f'{escape(source_url)}</a></div>'
            )
            self.url_paths[source_url] = path
        body = f"""
<p><a href="{escape(rel_link(path.parent, self.course_dir / 'index.html'))}">Back to course index</a></p>
<h1>{escape(title)}</h1>
<div class="panel muted">
  {source}
  <div>Archived: {escape(self.archive_timestamp)}</div>
</div>
{meta_table}
<article class="panel">
{body_html or '<p>No body content was returned by the Canvas API.</p>'}
</article>
"""
        write_text(path, html_shell(title, body))
        self.saved_html_files.append(path)

    def second_pass_rewrite(self) -> None:
        for path in self.saved_html_files:
            try:
                html = path.read_text(encoding="utf-8")
                rewritten = rewrite_known_local_links(
                    html,
                    self.target.domain,
                    self.target.course_id,
                    path,
                    self.resource_paths,
                    self.url_paths,
                )
                if rewritten != html:
                    write_text(path, rewritten)
            except OSError as exc:
                self.warn(f"Could not rewrite links in {path}: {exc}")

    def write_main_index(self, course: dict[str, Any]) -> None:
        course_name = course.get("name") or "Canvas Course"
        modules_html = "".join(
            f'<li><a href="{escape(rel_link(self.course_dir, Path(module["local_path"])))}">{escape(str(module.get("name") or "Module"))}</a></li>'
            for module in sorted(self.modules_index, key=lambda item: item.get("position") or 0)
            if module.get("local_path")
        )
        sections = [
            ("Syllabus", self.link_or_text(self.resource_paths.get("syllabus"), "Syllabus")),
            ("Modules", f"<ol>{modules_html or '<li>No modules were returned.</li>'}</ol>"),
            ("Pages", self.records_list(self.pages_index, "title")),
            ("Assignments", self.records_list(self.assignments_index, "name")),
            ("Files", self.records_list(self.files_index, "filename")),
            ("Discussions", self.records_list(self.discussion_index, "title")),
            ("Announcements", self.records_list(self.announcements_index, "title")),
        ]
        if self.include_submissions:
            sections.append(("Submissions", self.records_list(self.submissions_index, "assignment_id")))
        sections.extend(
            [
                ("External Links Not Downloaded", self.external_links_html()),
                ("Failed Downloads / Warnings", self.failures_html()),
                ("Coverage Report", '<p><a href="coverage_report.html">Open coverage report</a></p>'),
            ]
        )
        section_html = "".join(f'<section class="panel"><h2>{escape(title)}</h2>{content}</section>' for title, content in sections)
        summary = f"""
<h1>{escape(course_name)}</h1>
<div class="panel">
  <table>
    <tr><th>Course Code</th><td>{escape(str(course.get('course_code') or ''))}</td></tr>
    <tr><th>Course ID</th><td>{escape(self.target.course_id)}</td></tr>
    <tr><th>Canvas Course URL</th><td><a data-archive-source="true" href="{escape(self.target.original_url)}">{escape(self.target.original_url)}</a></td></tr>
    <tr><th>Archived</th><td>{escape(self.archive_timestamp)}</td></tr>
    <tr><th>Default View</th><td>{escape(str(course.get('default_view') or ''))}</td></tr>
    <tr><th>Workflow State</th><td>{escape(str(course.get('workflow_state') or ''))}</td></tr>
    <tr><th>Course Summary</th><td><a href="course_summary.html">course_summary.html</a></td></tr>
  </table>
</div>
{section_html}
"""
        write_text(self.course_dir / "index.html", html_shell(course_name, summary))
        self.saved_html_files.append(self.course_dir / "index.html")

    def link_or_text(self, path: Path | None, text: str) -> str:
        if path:
            return f'<p><a href="{escape(rel_link(self.course_dir, path))}">{escape(text)}</a></p>'
        readme = self.course_dir / "syllabus" / "README.txt"
        if readme.exists():
            return f'<p><a href="{escape(rel_link(self.course_dir, readme))}">Syllabus README</a></p>'
        return "<p>No syllabus was saved.</p>"

    def records_list(self, records: list[dict[str, Any]], label_key: str) -> str:
        items = []
        for record in records:
            label = record.get(label_key) or record.get("display_name") or record.get("id") or "Untitled"
            local_path = record.get("local_path")
            if local_path:
                items.append(f'<li><a href="{escape(rel_link(self.course_dir, Path(local_path)))}">{escape(str(label))}</a></li>')
            else:
                items.append(f"<li>{escape(str(label))}</li>")
        return f"<ul>{''.join(items) or '<li>None saved.</li>'}</ul>"

    def external_links_html(self) -> str:
        items = [
            f'<li><a class="external-link" target="_blank" rel="noopener noreferrer" href="{escape(item["url"])}">{escape(item.get("link_text") or item["url"])}</a> <span class="muted">External / requires internet: {escape(item.get("reason_not_downloaded") or "")}</span></li>'
            for item in self.external_links
        ]
        return f"<ul>{''.join(items) or '<li>No external links recorded.</li>'}</ul>"

    def failures_html(self) -> str:
        failures = [f'<li>{escape(str(item.get("url") or ""))}: {escape(str(item.get("error") or ""))}</li>' for item in self.failed_downloads]
        warnings = [f"<li>{escape(warning)}</li>" for warning in self.archive_warnings]
        return f"<h3>Failed Downloads</h3><ul>{''.join(failures) or '<li>None.</li>'}</ul><h3>Warnings</h3><ul>{''.join(warnings) or '<li>None.</li>'}</ul>"

    def write_audit_files(self) -> None:
        self.coverage["external_links_recorded"] = len(self.external_links)
        self.coverage["failed_downloads"] = len(self.failed_downloads)
        write_json(self.course_dir / "archive_log.json", self.archive_log)
        write_json(self.course_dir / "external_links.json", self.external_links)
        write_json(self.course_dir / "failed_downloads.json", self.failed_downloads)
        write_text(self.course_dir / "archive_warnings.txt", "\n".join(self.archive_warnings) if self.archive_warnings else "No warnings recorded.\n")
        write_json(self.course_dir / "coverage_report.json", self.coverage)
        write_text(self.course_dir / "coverage_report.html", coverage_report_html(self.coverage))
        write_json(self.course_dir / "link_graph.json", self.link_graph)
        write_json(self.course_dir / "files" / "files_index.json", self.files_index)
        write_json(self.course_dir / "assignments" / "assignments_index.json", self.assignments_index)
        write_json(self.course_dir / "discussions" / "discussion_index.json", self.discussion_index)
        write_json(self.course_dir / "announcements" / "announcements_index.json", self.announcements_index)

    def safe_paginated(self, path: str, params: Any, label: str) -> list[Any]:
        try:
            return self.client.get_paginated(path, params=params)
        except CanvasPermissionError as exc:
            self.warn(f"Could not access {label}: {exc}")
        except CanvasAPIError as exc:
            self.warn(f"Could not fetch {label}: {exc}")
        return []

    def used_set(self, directory: Path) -> set[str]:
        key = str(directory)
        if key not in self.used_names_by_dir:
            directory.mkdir(parents=True, exist_ok=True)
            self.used_names_by_dir[key] = {item.name for item in directory.iterdir()}
        return self.used_names_by_dir[key]

    def record_graph(
        self,
        resource: ResourceRef,
        classification: ResourceClassification,
        source_type: str,
        source_title: str,
        local_path: Path | None,
        status: str,
    ) -> None:
        self.link_graph["outgoing_links"].append(
            {
                "source_type": source_type,
                "source_title": source_title,
                "url": resource.absolute_url,
                "raw_url": resource.url,
                "link_text": resource.link_text,
                "classification": asdict(classification),
                "local_path": str(local_path) if local_path else None,
                "status": status,
            }
        )

    def record_external(self, resource: ResourceRef, classification: ResourceClassification) -> None:
        self.external_links.append(
            {
                "url": resource.absolute_url,
                "link_text": resource.link_text,
                "where_found": resource.where_found,
                "reason_not_downloaded": classification.reason,
                "classification": asdict(classification),
            }
        )

    def record_external_url(self, url: str, source_type: str, source_title: str, reason: str) -> None:
        if not url:
            return
        classification = normalize_canvas_url(url, self.target.domain, self.target.course_id)
        self.external_links.append(
            {
                "url": absolute_url(url, self.target.domain),
                "link_text": source_title,
                "where_found": source_type,
                "reason_not_downloaded": reason,
                "classification": asdict(classification),
            }
        )

    def record_failed(self, url: str | None, canvas_type: str, error: str) -> None:
        record = {"url": url, "canvas_type": canvas_type, "error": error}
        self.failed_downloads.append(record)
        self.log(canvas_type, url, None, "failed", error)

    def warn(self, message: str) -> None:
        print(f"Warning: {message}", file=sys.stderr)
        self.archive_warnings.append(message)

    def log(
        self,
        canvas_type: str,
        original_url: str | None,
        local_path: Path | None,
        status: str,
        error: str | None = None,
    ) -> None:
        record = {
            "canvas_type": canvas_type,
            "original_url": original_url,
            "local_path": str(local_path) if local_path else None,
            "status": status,
        }
        if error:
            record["error"] = error
        self.archive_log.append(record)


def token_missing_message(token_env: str) -> str:
    return f"""Canvas API token was not found.

macOS/Linux:
export {token_env}="your_token_here"

Windows PowerShell:
$env:{token_env}="your_token_here"
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive Canvas content through the Canvas REST API.")
    parser.add_argument("--course-url")
    parser.add_argument("--course-page-url")
    parser.add_argument("--domain")
    parser.add_argument("--course-id")
    parser.add_argument("--single-page-only", action="store_true")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    parser.add_argument("--download-external", type=parse_bool, default=False)
    parser.add_argument("--crawl-depth", type=int, default=2)
    parser.add_argument("--include-submissions", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    page_target: CoursePageTarget | None = None
    course_target: CourseTarget | None = None
    try:
        if args.course_page_url:
            page_target = parse_course_page_url(args.course_page_url)
            course_target = CourseTarget(
                domain=page_target.domain,
                course_id=page_target.course_id,
                original_url=f"{page_target.domain}/courses/{page_target.course_id}",
            )
        if args.course_url:
            course_target = parse_course_url(args.course_url)
        if args.domain and args.course_id:
            domain = normalize_domain(args.domain)
            course_target = CourseTarget(
                domain=domain,
                course_id=str(args.course_id),
                original_url=f"{domain}/courses/{args.course_id}",
            )
    except ValueError as exc:
        parser.error(str(exc))

    if args.single_page_only and not page_target:
        parser.error("--single-page-only requires --course-page-url.")
    if not course_target:
        parser.error("Provide --course-url, --course-page-url, or both --domain and --course-id.")

    token = os.environ.get(args.token_env)
    if not token:
        print(token_missing_message(args.token_env), file=sys.stderr)
        return 2

    try:
        if args.single_page_only:
            assert page_target is not None
            client = CanvasClient(page_target.domain, token, verbose=args.verbose)
            archiver = SinglePageArchiver(
                client=client,
                target=page_target,
                output_dir=Path(args.output_dir).expanduser().resolve(),
                download_external=args.download_external,
                dry_run=args.dry_run,
                overwrite=args.overwrite,
                verbose=args.verbose,
            )
            return archiver.archive()

        client = CanvasClient(course_target.domain, token, verbose=args.verbose)
        archiver = FullCourseArchiver(
            client=client,
            target=course_target,
            output_dir=Path(args.output_dir).expanduser().resolve(),
            page_target=page_target,
            download_external=args.download_external,
            include_submissions=args.include_submissions,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            crawl_depth=args.crawl_depth,
            verbose=args.verbose,
        )
        return archiver.archive()
    except CanvasPermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except CanvasAPIError as exc:
        print(f"Canvas API error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Archive cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
