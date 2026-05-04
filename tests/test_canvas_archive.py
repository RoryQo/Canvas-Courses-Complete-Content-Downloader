import tempfile
import unittest
from pathlib import Path

from canvas_archive import (
    classification_key,
    coverage_report_html,
    dedupe_filename,
    extract_html_resources,
    normalize_canvas_url,
    parse_course_url,
    parse_course_page_url,
    parse_link_header,
    render_module_index,
    rewrite_known_local_links,
    rewrite_local_links,
    sanitize_filename,
    unique_path,
)


class CanvasArchiveHelperTests(unittest.TestCase):
    def test_parse_course_url(self):
        target = parse_course_url("https://canvas.harvard.edu/courses/151500/?foo=bar")

        self.assertEqual(target.domain, "https://canvas.harvard.edu")
        self.assertEqual(target.course_id, "151500")
        self.assertEqual(target.original_url, "https://canvas.harvard.edu/courses/151500")

    def test_parse_course_page_url(self):
        target = parse_course_page_url(
            "https://canvas.harvard.edu/courses/151500/pages/week-1/?foo=bar"
        )

        self.assertEqual(target.domain, "https://canvas.harvard.edu")
        self.assertEqual(target.course_id, "151500")
        self.assertEqual(target.page_slug, "week-1")

    def test_sanitize_filename(self):
        self.assertEqual(
            sanitize_filename('Week 1: intro/readings?*.pdf'),
            "Week 1_ intro_readings_.pdf",
        )
        self.assertEqual(sanitize_filename(" . "), "untitled")

    def test_parse_link_header(self):
        header = (
            '<https://canvas.example.edu/api/v1/courses/1/files?page=2>; rel="next", '
            '<https://canvas.example.edu/api/v1/courses/1/files?page=5>; rel="last"'
        )

        links = parse_link_header(header)

        self.assertEqual(
            links["next"],
            "https://canvas.example.edu/api/v1/courses/1/files?page=2",
        )
        self.assertEqual(
            links["last"],
            "https://canvas.example.edu/api/v1/courses/1/files?page=5",
        )

    def test_duplicate_filename_handling(self):
        used = set()

        self.assertEqual(dedupe_filename("Lecture.pdf", used), "Lecture.pdf")
        self.assertEqual(dedupe_filename("Lecture.pdf", used), "Lecture (2).pdf")
        self.assertEqual(dedupe_filename("Lecture.pdf", used), "Lecture (3).pdf")

    def test_unique_path_considers_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "Lecture.pdf").write_text("existing", encoding="utf-8")

            path = unique_path(directory, "Lecture.pdf", {"Lecture.pdf"})

            self.assertEqual(path.name, "Lecture (2).pdf")

    def test_html_resource_extraction(self):
        html = """
        <a href="/courses/1/files/10/download">file</a>
        <iframe src="https://example.com/embed"></iframe>
        <img src="/images/photo.png">
        <video src="/video.mp4"><source src="/video.webm"></video>
        <audio src="/audio.mp3"></audio>
        <embed src="/embed.pdf">
        <object data="/slides.pdf"></object>
        """

        resources = extract_html_resources(html, "https://canvas.harvard.edu", "week-1")
        urls = {resource.url for resource in resources}

        self.assertEqual(len(resources), 8)
        self.assertIn("/courses/1/files/10/download", urls)
        self.assertIn("https://example.com/embed", urls)
        self.assertIn("/slides.pdf", urls)

    def test_normalize_canvas_file_url(self):
        classification = normalize_canvas_url(
            "https://canvas.harvard.edu/courses/151500/files/987/download?wrap=1&verifier=abc",
            "https://canvas.harvard.edu",
            "151500",
        )

        self.assertEqual(classification.kind, "canvas_file")
        self.assertEqual(classification.id_or_slug, "987")
        self.assertTrue(classification.downloadable_by_api)
        self.assertFalse(classification.should_record_only)

    def test_normalize_canvas_page_url(self):
        classification = normalize_canvas_url(
            "/courses/151500/pages/week-2",
            "https://canvas.harvard.edu",
            "151500",
        )

        self.assertEqual(classification.kind, "canvas_page")
        self.assertEqual(classification.id_or_slug, "week-2")
        self.assertTrue(classification.should_record_only)

    def test_normalize_external_protected_url(self):
        classification = normalize_canvas_url(
            "https://login.ezp-prod1.hul.harvard.edu/some-reading",
            "https://canvas.harvard.edu",
            "151500",
        )

        self.assertEqual(classification.kind, "external_protected")
        self.assertTrue(classification.should_record_only)

    def test_local_link_rewriting(self):
        html = '<p><a href="/courses/151500/files/987/download">PDF</a><a href="https://example.com">External</a></p>'
        resources = extract_html_resources(html, "https://canvas.harvard.edu", "week-1")
        entries = []
        for resource in resources:
            entries.append(
                {
                    "resource": resource,
                    "classification": normalize_canvas_url(
                        resource.absolute_url,
                        "https://canvas.harvard.edu",
                        "151500",
                    ),
                }
            )

        rewritten = rewrite_local_links(
            html,
            "https://canvas.harvard.edu",
            {"/courses/151500/files/987/download": "../linked_files/pages/reading.pdf"},
            entries,
        )

        self.assertIn('href="../linked_files/pages/reading.pdf"', rewritten)
        self.assertIn('class="external-link"', rewritten)
        self.assertIn('target="_blank"', rewritten)

    def test_local_path_mapping_keys(self):
        page = normalize_canvas_url("/courses/151500/pages/week-1", "https://canvas.harvard.edu", "151500")
        file_ref = normalize_canvas_url("/files/44/download", "https://canvas.harvard.edu", "151500")
        assignment = normalize_canvas_url("/courses/151500/assignments/7", "https://canvas.harvard.edu", "151500")
        discussion = normalize_canvas_url("/courses/151500/discussion_topics/9", "https://canvas.harvard.edu", "151500")

        self.assertEqual(classification_key(page), "page:week-1")
        self.assertEqual(classification_key(file_ref), "file:44")
        self.assertEqual(classification_key(assignment), "assignment:7")
        self.assertEqual(classification_key(discussion), "discussion:9")

    def test_module_index_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            module_dir = Path(tmp)
            local_page = module_dir / "page.html"
            local_page.write_text("x", encoding="utf-8")
            html = render_module_index(
                "Week 1",
                [
                    {"position": 1, "type": "Page", "title": "Overview", "local_path": str(local_page)},
                    {"position": 2, "type": "ExternalUrl", "title": "Library", "external_url": "https://hollis.harvard.edu"},
                ],
                module_dir,
            )

        self.assertIn("Week 1", html)
        self.assertIn("Overview", html)
        self.assertIn("external-link", html)

    def test_second_pass_link_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html_path = root / "pages" / "week.html"
            local_assignment = root / "assignments" / "Essay.html"
            html_path.parent.mkdir()
            local_assignment.parent.mkdir()
            rewritten = rewrite_known_local_links(
                '<a href="https://canvas.harvard.edu/courses/151500/assignments/7">Essay</a>',
                "https://canvas.harvard.edu",
                "151500",
                html_path,
                {"assignment:7": local_assignment},
            )

        self.assertIn('href="../assignments/Essay.html"', rewritten)

    def test_coverage_report_generation(self):
        html = coverage_report_html({"files_discovered": 2, "files_downloaded": 1})

        self.assertIn("Coverage Report", html)
        self.assertIn("Files Discovered", html)


if __name__ == "__main__":
    unittest.main()
