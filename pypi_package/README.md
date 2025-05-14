# Canvas Downloader

**Canvas Downloader** is a lightweight Python package for downloading course files and module content from the Canvas Learning Management System (LMS).  
It supports both **Mac** and **Windows** users, with functions designed for platform-specific requirements.

---

##  Fatures

- Download **all files** uploaded to your Canvas courses
- Download **linked module pages** (PDFs or file attachments)
- Handle **multiple courses** at once
- Automatically organize downloaded content by **course ID**
- Windows users supported via `wkhtmltopdf` integration
- Mac users supported with native tools

---

## Installation

First, install required Python packages:

(*Your bash install example here*)

> **Note for Windows users:**  
> You must install [wkhtmltopdf](https://wkhtmltopdf.org/downloads.html) separately.  
> After installing, make sure the path to `wkhtmltopdf.exe` is configured in the script (default is `C:/Program Files/wkhtmltopdf/bin/wkhtmltopdf.exe`).

---

## Usage

### Obtaining Your Canvas API Token

To use the Canvas Downloader package, you must have a valid Canvas LMS API token.  
This token allows the package to authenticate with your Canvas account and download your authorized course content.

#### How to Generate a Canvas API Token

1. **Log into Canvas** through your institution's Canvas portal.
2. **Go to Account Settings**:
   - Click your profile picture (top left).
   - Click **Settings**.
3. **Create a New Access Token**:
   - Scroll down to **Approved Integrations** or **Access Tokens** section.
   - Click **+ New Access Token** or **Create New Token**.
4. **Fill in the Details**:
   - **Purpose:** (Example: "Canvas Downloader")
   - **Expires:** (Optional, but recommended for security)
5. **Copy the Token**:
   - After creation, **immediately copy** the token shown.
   - You will **not** be able to view it again later!
6. **Save it securely** — you will pass this token into the downloader functions in your Python code.

---

## Function Documentation


### Import the downloader

```python
from canvas_downloader.downloader import (
    download_specific_courses_mac,
    download_all_courses_mac,
    download_specific_courses_windows,
    download_all_courses_windows)
```

### download_specific_courses_mac(course_ids, token, output_dir)

```python
download_specific_courses_mac(course_ids=[12345, 67890], token="YOUR_CANVAS_TOKEN", output_dir="./downloads")
```

**Purpose:**  
Download files and module content for specified courses (Mac users).

**Inputs:**
- `course_ids` (list of int): List of Canvas course IDs to download.
- `token` (str): Canvas API access token.
- `output_dir` (str or Path): Directory to save downloaded content.

**Outputs:**
- Downloads course files and module-linked files into structured folders.

---

### download_all_courses_mac(token, output_dir)

```python
download_all_courses_mac(token="YOUR_CANVAS_TOKEN", output_dir="./downloads")
```


**Purpose:**  
Download files and module content from all active courses (Mac users).

**Inputs:**
- `token` (str): Canvas API access token.
- `output_dir` (str or Path): Directory to save downloaded content.

**Outputs:**
- Downloads all course files and module-linked files into structured folders.

---

### download_specific_courses_windows(course_ids, token, output_dir)

```python
download_specific_courses_windows(course_ids=[12345, 67890], token="YOUR_CANVAS_TOKEN", output_dir="./downloads")
```

**Purpose:**  
Download files and module content for specified courses (Windows users).

**Inputs:**
- `course_ids` (list of int): List of Canvas course IDs to download.
- `token` (str): Canvas API access token.
- `output_dir` (str or Path): Directory to save downloaded content.

**Outputs:**
- Downloads course files and saves module pages as PDFs (using wkhtmltopdf).

---

### download_all_courses_windows(token, output_dir)

```python
download_all_courses_windows(token="YOUR_CANVAS_TOKEN", output_dir="./downloads")
```

**Purpose:**  
Download files and module content from all active courses (Windows users).

**Inputs:**
- `token` (str): Canvas API access token.
- `output_dir` (str or Path): Directory to save downloaded content.

**Outputs:**
- Downloads all course files and saves module pages as PDFs (using wkhtmltopdf).

---

## Quick Function Summary

| Function Name | Platform | Downloads | Notes |
|:--|:--|:--|:--|
| `download_specific_courses_mac` | Mac | Specific courses | Files + module links |
| `download_all_courses_mac` | Mac | All courses | Files + module links |
| `download_specific_courses_windows` | Windows | Specific courses | Files + module links as PDFs |
| `download_all_courses_windows` | Windows | All courses | Files + module links as PDFs |

---

## Project Folder Structure

```plaintext
canvas_downloader/
├── canvas_downloader/
│   ├── __init__.py
│   ├── downloader_mac.py         # Mac-specific course downloading logic
│   ├── downloader_windows.py     # Windows-specific course downloading logic
│   ├── downloader.py              # Wrapper exposing clean public functions
├── tests/
│   └── test_downloader.py         # (Optional) Future test scripts
├── pyproject.toml                 # Build system config
├── README.md                      # Project documentation
└── .gitignore                     # Git ignore rules
