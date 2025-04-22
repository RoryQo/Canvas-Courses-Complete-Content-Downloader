# Download All Canvas Courses Content 

This notebook allows you to download everything from your Canvas account—including course files, assignments, pages, modules, submissions, and embedded or linked documents within assignments and modules.

It works for all courses you have access to, including both current and past enrollments. Everything is saved directly to your computer in clearly labeled folders—one for each course—for easy offline access.

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

## Setup Instructions

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

To use this tool, install the following Python packages:

- `requests` – for making API calls to Canvas
- `tqdm` – for showing progress bars during downloads
- `beautifulsoup4` – for parsing HTML from Canvas content
- `html2text` – for converting HTML into plain text (used internally)
- `pdfkit` – for converting Canvas pages and assignments to PDF


You can install them all at once with:

```python
!pip install requests tqdm beautifulsoup4 html2text pdfkit
```


### 4. Install wkhtmltopdf

Download and install `wkhtmltopdf` from [https://wkhtmltopdf.org/downloads.html](https://wkhtmltopdf.org/downloads.html)
The notebook includes the default executable path:

```
C:/Program Files/wkhtmltopdf/bin/wkhtmltopdf.exe
```


This is the correct path for most Windows installations. If you install `wkhtmltopdf` in a different location or use a different operating system, you may need to update the path manually in the notebook.



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
