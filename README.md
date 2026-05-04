# Download All Canvas Courses Content  


**No programming experience needed.** Just follow the steps outlined in the README and only edit the clearly marked cells.

This notebook allows you to download everything from your Canvas account—including course files, assignments, pages, modules, submissions, and embedded or linked documents within assignments and modules.

It works for all courses you have access to, including both current and past enrollments. Everything is saved directly to your computer in clearly labeled folders, one for each course, for easy offline access.

**Features Overview**

- Downloads all enrolled courses (past and current)
- Saves: 
  - Uploaded course files
  - Assignment descriptions (as PDFs)
  - Pages and modules (as PDFs)
  - Submitted assignment attachments
  - All embedded or linked files inside assignments and modules (any file type)
- Automatically organizes content into course-specific folders

---

## Phase 1: Archive a single Canvas Page

The Phase 1 CLI archives one Canvas Page through the Canvas REST API under
`/api/v1`. It saves the page as local HTML, downloads Canvas-hosted files linked
from that page, rewrites those file links to local paths, and records
external/protected links without trying to bypass logins, paywalls, library
proxies, or streaming protections.

### 1. Install the Python packages

```bash
python -m pip install -r requirements.txt
```

### 2. Set your Canvas API token

Do not paste your token into `canvas_archive.py`. Store it in an environment
variable for the terminal session.

macOS/Linux:

```bash
export CANVAS_API_TOKEN="your_token_here"
```

Windows PowerShell:

```powershell
$env:CANVAS_API_TOKEN="your_token_here"
```

Treat this token like a password. Revoke it from Canvas when you no longer
need it.

### 3. Run the archive

Dry run first. This validates the token, fetches course/page metadata, extracts
and classifies links, and does not download file bodies.

```bash
python canvas_archive.py --course-page-url "https://canvas.harvard.edu/courses/151500/pages/week-1" --single-page-only --dry-run
```

Archive the page:

```bash
python canvas_archive.py --course-page-url "https://canvas.harvard.edu/courses/151500/pages/week-1" --single-page-only
```

Optional output directory:

```bash
python canvas_archive.py --course-page-url "https://canvas.harvard.edu/courses/151500/pages/week-1" --single-page-only --output-dir canvas_all_content
```

Open the generated local page:

```text
canvas_all_content/<Course Name - 151500>/index.html
```

Canvas-hosted linked files should work offline after the archive completes.
External/protected links are recorded in `external_links.json` and may still
require internet access and a Canvas, library, or provider login.

## Archive a full Canvas course

Full-course mode uses Canvas API endpoints under `/api/v1` to archive the
course content your token can access. It uses Canvas Modules as the main local
navigation backbone and writes a course `index.html` for offline browsing.

### 1. Install the Python packages

```bash
python -m pip install -r requirements.txt
```

### 2. Set your Canvas API token

macOS/Linux:

```bash
export CANVAS_API_TOKEN="your_token_here"
```

Windows PowerShell:

```powershell
$env:CANVAS_API_TOKEN="your_token_here"
```

Treat the token like a password. Revoke it in Canvas when you no longer need it.

### 3. Dry run

```bash
python canvas_archive.py --course-url "https://canvas.harvard.edu/courses/151500" --dry-run
```

### 4. Archive

```bash
python canvas_archive.py --course-url "https://canvas.harvard.edu/courses/151500"
```

Equivalent form:

```bash
python canvas_archive.py --domain "https://canvas.harvard.edu" --course-id 151500
```

Open:

```text
canvas_all_content/<Course Name - 151500>/index.html
```

Canvas-hosted/API-accessible files, pages, assignments, discussions,
announcements, syllabus content, and module indexes should be available offline
where your account has permission to access them. External/protected resources
are recorded as links and may require internet access or login. The archiver
does not try to bypass authentication, DRM, paywalls, library proxy
restrictions, LTI tools, or streaming protections.

---

## Setup Instructions

The notebooks are still included for compatibility, but the Phase 1 command-line
single-page archive above is the recommended path for new use.

### 1. Generate a Canvas API Token 

To authorize the script to access your account:

1. Log in to your institution’s Canvas website.
2. In the left-hand sidebar, click **Account** > **Settings**.
3. Scroll down to the section labeled **Approved Integrations** or **Access Tokens**.
4. Click **+ New Access Token**.
5. Enter a name and optional expiry date for the token, then click **Generate Token**.
6. Copy the access token value and paste it into the notebook where indicated.

```
API_TOKEN = 'Your_API_Key'
```

**Important:** Save your token immediately. You will not be able to see it again once you leave the page.

### 2. Update the Canvas Domain

The script defaults to the University of Pittsburgh’s Canvas domain (`canvas.pitt.edu`). Be sure to replace `pitt` with your own institution’s Canvas subdomain (e.g., `harvard`, `berkeley`, `utexas`) wherever the domain appears.

```
CANVAS_DOMAIN = 'https://canvas.pitt.edu'
```

### 3. Install Required Packages

To use the command-line archiver, install the following Python packages:

- `requests` – for making API calls to Canvas
- `tqdm` – for showing progress bars during downloads
- `beautifulsoup4` – for parsing HTML from Canvas content


You can install them all at once with:

```bash
python -m pip install -r requirements.txt
```

The older notebooks may still require `pdfkit` and `wkhtmltopdf` if you want
their PDF conversion workflow. The new `canvas_archive.py` output is HTML-first
because it preserves local links better.
**Mac Users**




pdfkit auto installation in Jupyter Notebook may auto-install in the wrong environment. Use this command to ensure it is in the correct place for usage:

```
!/opt/miniconda3/envs/ba2/bin/python -m pip install pdfkit
```

### 4. Install wkhtmltopdf

Download and install `wkhtmltopdf` from [https://wkhtmltopdf.org/downloads.html](https://wkhtmltopdf.org/downloads.html)
The notebook includes the default executable path:

```
C:/Program Files/wkhtmltopdf/bin/wkhtmltopdf.exe
```


This is the correct path for most Windows installations. If you install `wkhtmltopdf` in a different location or use a different operating system, you may need to update the path manually in the notebook.

**Mac Users**

- **1.** The automatic download path is different, update your file path in the script to:

    ```python
    pdfkit_config = pdfkit.configuration(wkhtmltopdf='/usr/local/bin/wkhtmltopdf')
    ```

- **2.** If macOS blocks `whpdftohtml` with a message like "`whpdftohtml` can't be opened because it is from an unidentified developer," here's how to bypass the warning and use it anyway:

    - Go to `System Settings > Privacy & Security`
    - Scroll to the bottom of the `Security` section 
      There should be a message like:  
      > `'whpdftohtml' was blocked from use because it is not from an identified developer.`
    - Click the "Open Anyway" button
    - In the confirmation dialog, click "Open" 
      macOS will now allow the file to run


## How to Use

1. Open the notebook.
2. Paste your Canvas token where instructed.
3. Replace the Canvas domain if needed.
4. Run the notebook from top to bottom.
5. Your content will be downloaded into a folder on your computer, with each course in its own subfolder.


## Output

### Folder Structure

All downloaded files are saved to a local directory named canvas_all_content, with one subfolder per course. Original filenames and extensions are preserved.

```
canvas_all_content/
├── Course Name A/
│   ├── lecture1.pdf
│   ├── page - Syllabus.html
│   ├── assignment - Essay.html
│   ├── module - Week 1 Overview.html
│   └── submission - final_essay.pdf
├── Course Name B/
│   └── ...
```

### Notes

- Some **assignment and module PDFs may appear mostly blank**. This is expected behavior:
  - Modules in Canvas are often used to organize links to readings, files, and other resources, rather than contain standalone instructional content.
  - Assignment pages may also be empty unless the instructor wrote detailed descriptions directly into Canvas.

- These PDFs are still included in the download because they often contain **embedded or linked files** that are important—such as:
  - Required readings
  - Data files
  - Code notebooks
  - External tools or references

- By processing all assignments and modules, the downloader ensures that **no embedded content is missed**, even if it’s hidden inside otherwise empty-looking pages.


## Security and Privacy

- Your Canvas access token is used exclusively for authenticating with the Canvas API during your session.
- All downloaded content is saved locally to your computer—no data is stored or transmitted externally.
- You can revoke your token at any time from the **Account > Settings** page within Canvas.



## Why Use This Tool?

Most institutions disable Canvas access shortly after graduation, making it difficult to retrieve valuable course materials later. This tool ensures you retain a complete offline archive of your academic history—including content that is often missed, such as embedded files, linked readings, and submitted work.



## Support and Contributions

Have feedback, feature suggestions, or found a bug?  
Feel free to open an issue or submit a pull request. Contributions are always welcome and appreciated!
