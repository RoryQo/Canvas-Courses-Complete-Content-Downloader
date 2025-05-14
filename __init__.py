"""
Canvas Downloader

Provides utilities to download course files and module content from Canvas LMS 
for Mac and Windows users.
"""

from .downloader import (
    download_specific_courses_mac,
    download_all_courses_mac,
    download_specific_courses_windows,
    download_all_courses_windows
)

__all__ = [
    "download_specific_courses_mac",
    "download_all_courses_mac",
    "download_specific_courses_windows",
    "download_all_courses_windows",
]
