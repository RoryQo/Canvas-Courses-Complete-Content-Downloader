import tempfile
import unittest
from pathlib import Path

from canvas_archive import (
    dedupe_filename,
    extract_html_resources,
    parse_course_url,
    parse_link_header,
    sanitize_filename,
    unique_path,
)


class CanvasArchiveHelperTests(unittest.TestCase):
    def test_parse_course_url(self):
        domain, course_id = parse_course_url(
            "https://canvas.harvard.edu/courses/146553/modules?foo=bar"
        )

        self.assertEqual(domain, "https://canvas.harvard.edu")
        self.assertEqual(course_id, "146553")

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

            path = unique_path(directory, "Lecture.pdf", set())

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

        resources = extract_html_resources(html)
        urls = {resource.url for resource in resources}

        self.assertEqual(len(resources), 8)
        self.assertIn("/courses/1/files/10/download", urls)
        self.assertIn("https://example.com/embed", urls)
        self.assertIn("/slides.pdf", urls)


if __name__ == "__main__":
    unittest.main()
