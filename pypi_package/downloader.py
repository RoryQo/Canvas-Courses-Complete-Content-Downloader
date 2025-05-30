from . import downloader_mac
from . import downloader_windows

def download_specific_courses_mac(course_ids, token, output_dir, base_url="https://canvas.instructure.com/api/v1"):
    return downloader_mac.download_specific_courses(course_ids, token, output_dir, base_url)

def download_all_courses_mac(token, output_dir, base_url="https://canvas.instructure.com/api/v1"):
    return downloader_mac.download_all_courses(token, output_dir, base_url)

def download_specific_courses_windows(course_ids, token, output_dir, base_url="https://canvas.instructure.com/api/v1"):
    return downloader_windows.download_specific_courses(course_ids, token, output_dir, base_url)

def download_all_courses_windows(token, output_dir, base_url="https://canvas.instructure.com/api/v1"):
    return downloader_windows.download_all_courses(token, output_dir, base_url)
