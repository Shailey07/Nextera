# ⚡ Nextera

<div align="center">

### 🚀 AI-Powered Coursera Course Automation

**Nextera** is a Chrome extension that streamlines repetitive Coursera course workflows using browser automation, a Python native-messaging host, and LLM-powered assistance.

<br>

[![Chrome Extension](https://img.shields.io/badge/Chrome-Extension-4285F4?logo=googlechrome\&logoColor=white)](https://www.google.com/chrome/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python\&logoColor=white)](https://www.python.org/)
[![AI](https://img.shields.io/badge/AI-Groq%20%2B%20Gemini-8B5CF6)](https://groq.com/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows\&logoColor=white)](https://www.microsoft.com/windows/)
[![GitHub](https://img.shields.io/badge/GitHub-Shailey07-181717?logo=github)](https://github.com/shailey07)

<br>

**Made with ❤️ by [Shailendra Meghwal](https://github.com/shailey07)**

</div>

---

## 📌 Table of Contents

* [What is Nextera?](#-what-is-nextera)
* [Features](#-features)
* [How It Works](#-how-it-works)
* [Tech Stack](#-tech-stack)
* [Prerequisites](#-prerequisites)
* [Installation](#️-installation)
* [API Key Setup](#-api-key-setup)
* [Usage](#️-usage)
* [Keyboard Shortcuts](#️-keyboard-shortcuts)
* [CLI Testing](#-cli-testing)
* [Troubleshooting](#-troubleshooting)
* [Project Structure](#-project-structure)
* [Security](#-security)
* [Disclaimer](#️-disclaimer)
* [Author](#-author)

---

# 🧠 What is Nextera?

**Nextera** is a Chrome extension built to automate repetitive parts of Coursera courses from a simple popup interface.

It combines:

* Chrome Extension APIs
* Chrome Native Messaging
* Python
* Coursera's internal APIs
* Groq / Gemini LLMs
* Automated course processing

The goal is to provide a single interface for handling different types of course content.

### Supported Workflow

```text
Coursera Course
      │
      ▼
   Nextera
      │
      ├── 🎬 Videos
      ├── 📚 Readings
      ├── 🧠 Practice Quizzes
      ├── 📝 Graded Assignments
      ├── 💬 Discussions
      └── 🔗 Shareable Links
```

---

# ✨ Features

| Feature                   | Description                                 |
| ------------------------- | ------------------------------------------- |
| 🎬 **Complete Videos**    | Automatically process supported video items |
| 📚 **Complete Readings**  | Process supported reading materials         |
| 🧠 **Practice Quizzes**   | AI-assisted quiz solving                    |
| 📝 **Graded Assignments** | AI-assisted assignment workflow             |
| 💬 **Discussions**        | Generate responses for discussion prompts   |
| 🔗 **Shareable Links**    | Generate shareable links for assignments    |
| ⚡ **Complete Course**     | Process videos + readings                   |
| 🤖 **Solve with AI**      | Run the broader AI-assisted workflow        |
| 🛑 **Stop Anytime**       | Cancel the active process                   |
| ⚙️ **Settings**           | Manage API keys and model configuration     |
| 📊 **Live Progress**      | View current item and processing logs       |

---

# ⚙️ How It Works

```text
┌──────────────────────────────┐
│       Chrome Extension       │
│         Nextera UI           │
└──────────────┬───────────────┘
               │
               │ Chrome Native Messaging
               ▼
┌──────────────────────────────┐
│      Python Native Host      │
│       nextera_native.py      │
└──────────────┬───────────────┘
               │
       ┌───────┴────────┐
       ▼                ▼
┌─────────────┐   ┌──────────────┐
│  Coursera   │   │  LLM APIs    │
│ Internal API│   │ Groq / Gemini│
└─────────────┘   └──────────────┘
```

### Architecture

**Frontend**

Chrome Extension → Popup → Settings → Course Controls

**Backend**

Python Native Host → Course Processing → Coursera API

**AI Layer**

Groq / Gemini → Question Analysis → Generated Responses

---

# 🛠️ Tech Stack

### Frontend

* Chrome Extension
* JavaScript
* HTML
* CSS
* Chrome Extension APIs
* Chrome Native Messaging

### Backend

* Python 3.10+
* HTTPX
* Pydantic
* Loguru
* Browser Cookie3

### AI

* Groq API
* Google Gemini API

### Integration

* Coursera internal APIs
* Native Messaging Host
* Windows Registry

---

# 📋 Prerequisites

Before installing Nextera, make sure you have:

* **Windows 10/11**
* **Google Chrome**
* **Python 3.10+**
* **Git** *(optional)*
* Coursera account
* At least one LLM API key

### Install Python

Download Python:

https://python.org

During installation, make sure:

```text
☑ Add Python to PATH
```

Verify installation:

```bat
python --version
```

Expected:

```text
Python 3.10.x or higher
```

---

# 🛠️ Installation

## 1. Clone the Repository

Open CMD:

```bat
cd %USERPROFILE%\Desktop
git clone https://github.com/shailey07/nextera.git
cd nextera
```

Or download the ZIP from:

https://github.com/shailey07/nextera

Extract it to:

```text
Desktop\Nextera
```

---

## 2. Install Dependencies

```bat
cd "%USERPROFILE%\Desktop\Nextera\native"
pip install -r requirements.txt
```

Verify:

```bat
pip show httpx loguru pydantic browser-cookie3 google-genai
```

---

# 🌐 3. Load Extension in Chrome

Open:

```text
chrome://extensions/
```

Then:

1. Enable **Developer mode**
2. Click **Load unpacked**
3. Select the `Nextera` folder
4. Make sure the selected folder contains `manifest.json`
5. Copy the **Extension ID**

Example:

```text
abcdefghijklmnopabcdefghijklmnop
```

Keep this ID—you will need it for Native Messaging.

---

# 🔌 4. Register Native Messaging Host

Open **CMD as Administrator**.

Run:

```bat
REG ADD "HKCU\Software\Google\Chrome\NativeMessagingHosts\com.nextera.fasttrack" /ve /t REG_SZ /d "%LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\com.nextera.fasttrack.json" /f
```

Create the Native Messaging configuration:

```bat
notepad "%LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\com.nextera.fasttrack.json"
```

Paste:

```json
{
  "name": "com.nextera.fasttrack",
  "description": "Nextera Native Host",
  "path": "C:\\Users\\YOUR_USERNAME\\Desktop\\Nextera\\native\\nextera_launcher.bat",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://YOUR_EXTENSION_ID/"
  ]
}
```

Replace:

```text
YOUR_USERNAME
```

with your Windows username.

Replace:

```text
YOUR_EXTENSION_ID
```

with your Chrome extension ID.

---

# 🧩 5. Verify Launcher

Run:

```bat
type "%USERPROFILE%\Desktop\Nextera\native\nextera_launcher.bat"
```

It should contain something similar to:

```bat
@echo off
"C:\Users\YOUR_USERNAME\AppData\Local\Programs\Python\Python311\python.exe" "C:\Users\YOUR_USERNAME\Desktop\Nextera\native\nextera_native.py"
```

If your Python path is different:

```bat
where python
```

Then update the `.bat` file.

---

# 🔄 6. Restart Chrome

Completely close Chrome.

Then:

1. Press `Ctrl + Shift + Esc`
2. Open **Task Manager**
3. End remaining Chrome processes
4. Start Chrome again

---

# ✅ 7. Test Nextera

Open a Coursera course:

```text
https://www.coursera.org/learn/YOUR-COURSE/home/welcome
```

Click the Nextera icon.

You should see:

```text
🟢 Ready
```

---

# 🔑 API Key Setup

Nextera requires at least one LLM API key for AI-powered features.

You can configure:

* Groq
* Gemini

---

## ⚡ Groq

Create an API key:

https://console.groq.com

Typical format:

```text
gsk_...
```

### Available Models

```text
openai/gpt-oss-20b
openai/gpt-oss-120b
```

Recommended configuration:

```json
{
  "groq_model": "openai/gpt-oss-20b"
}
```

---

## ♊ Gemini

Create an API key:

https://aistudio.google.com/apikey

Typical format:

```text
AIzaSy...
```

Available models:

```text
gemini-2.5-flash
gemini-2.5-flash-lite
gemini-3.6-flash
```

Recommended:

```json
{
  "gemini_model": "gemini-2.5-flash"
}
```

---

# ⚙️ Configure API Keys

Open Nextera:

```text
Nextera → ⚙ Settings
```

Enter:

```text
Groq API Key
Gemini API Key
```

Then:

```text
Save Settings
```

You only need one provider, although configuring both allows fallback options.

---

# 🗂️ Manual Configuration

Configuration file:

```bat
notepad "%USERPROFILE%\.nextera\config.json"
```

Example:

```json
{
  "cookies": {},
  "groq_api_key": "gsk_YOUR_KEY",
  "groq_model": "openai/gpt-oss-20b",
  "gemini_api_key": "AIzaSy...",
  "gemini_model": "gemini-2.5-flash",
  "perplexity_api_key": "",
  "perplexity_model": "sonar-pro"
}
```

> ⚠️ Never upload your `config.json` containing API keys or cookies to GitHub.

---

# ▶️ Usage

## Step 1 — Open Coursera

Open:

```text
https://www.coursera.org/learn/YOUR-COURSE/home/welcome
```

---

## Step 2 — Open Nextera

Click the extension icon.

Or use:

```text
Alt + A
```

---

## Step 3 — Select a Mode

### ⚡ Complete Course

Processes:

```text
Videos
+
Readings
```

---

### 🤖 Solve with AI

Runs the broader AI workflow:

```text
Videos
Readings
Quizzes
Assignments
Discussions
```

---

### 🎬 Videos

Only processes video content.

---

### 📚 Readings

Only processes reading content.

---

### 🧠 Quizzes

Processes supported practice quizzes.

---

### 📝 Graded

Processes supported graded assignments.

---

### 💬 Discussions

Processes supported discussion prompts.

---

### 🔗 Shareable Link

Generates a shareable link for the current assignment where supported.

---

# 📊 Progress

During processing, Nextera displays information such as:

```text
Status: Processing...

Current Item:
Introduction to Machine Learning

Logs:
Processing...
```

When finished:

```text
Status: Complete!
```

You can stop the process at any time using:

```text
🛑 Stop
```

---

# 🔄 Verify Course Progress

After Nextera finishes:

1. Refresh Coursera with `F5`
2. Check completion indicators
3. Check course progress
4. Allow some time for progress synchronization

---

# ⌨️ Keyboard Shortcuts

| Shortcut  | Action       |
| --------- | ------------ |
| `Alt + A` | Open Nextera |

---

# 🧪 CLI Testing

CLI commands can help diagnose installation problems.

## Test 1 — Import Check

```bat
cd "%USERPROFILE%\Desktop\Nextera\native"

python -c "import sys; sys.path.insert(0, '.'); from nextera.main import Nextera; print('Import OK')"
```

Expected:

```text
Import OK
```

---

## Test 2 — Videos

```bat
python -c "import sys; sys.path.insert(0, '.'); from nextera.main import Nextera; s = Nextera('YOUR-COURSE-SLUG', True, mode='videos'); s.get_course()"
```

---

## Test 3 — AI Mode

```bat
python -c "import sys; sys.path.insert(0, '.'); from nextera.main import Nextera; s = Nextera('YOUR-COURSE-SLUG', True, mode='llm'); s.get_course()"
```

---

## Test 4 — Configuration

```bat
type "%USERPROFILE%\.nextera\config.json"
```

---

## Test 5 — Native Host

```bat
type "%LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\com.nextera.fasttrack.json"
```

---

## Test 6 — Registry

```bat
REG QUERY "HKCU\Software\Google\Chrome\NativeMessagingHosts\com.nextera.fasttrack" /ve
```

---

# 🧰 Troubleshooting

## ❌ Native Host Has Exited

### Cause

The Python native host may have crashed.

Run:

```bat
cd "%USERPROFILE%\Desktop\Nextera\native"

python -c "import sys; sys.path.insert(0, '.'); from nextera.main import Nextera; print('Import OK')"
```

---

## ❌ Native Messaging Host Not Found

Check:

```bat
type "%LOCALAPPDATA%\Google\Chrome\User Data\NativeMessagingHosts\com.nextera.fasttrack.json"
```

Then:

```bat
REG QUERY "HKCU\Software\Google\Chrome\NativeMessagingHosts\com.nextera.fasttrack" /ve
```

Finally restart Chrome completely.

---

## ❌ Cookies Are Invalid

First log into Coursera normally.

Then close Chrome completely.

Delete:

```bat
del "%USERPROFILE%\.nextera\config.json"
```

Restart Nextera.

---

## ❌ 429 Rate Limit

This means the selected API provider has temporarily rate-limited requests.

Possible solutions:

* Wait for the rate-limit window
* Switch provider
* Switch model
* Reduce request frequency

---

## ❌ No API Key Specified

Open:

```text
Nextera → Settings
```

Add a valid:

```text
Groq API Key
```

or:

```text
Gemini API Key
```

---

## ❌ ImportError

Example:

```text
ImportError: cannot import name 'X'
```

Clear Python cache:

```bat
cd "%USERPROFILE%\Desktop\Nextera\native"

for /d /r . %d in (__pycache__) do @if exist "%d" rd /s /q "%d"

del /s /q *.pyc 2>nul
```

Then restart Nextera.

---

## ❌ Forum Rate Limit

If discussion processing is rate-limited, check:

```text
native/nextera/discussion/solver.py
```

The project can use a delay such as:

```python
random_delay(45.0, 90.0)
```

---

## ❌ No More Attempts Remaining

Some Coursera assessments enforce attempt limits.

If the course has reached its limit, you must wait until the platform restores additional attempts according to the course's rules.

---

## ❌ “Coursera course kholo”

Nextera cannot detect a supported course page.

Make sure you are on a URL similar to:

```text
https://www.coursera.org/learn/COURSE-SLUG/...
```

---

## ❌ Progress Shows 0%

Coursera may take some time to synchronize course progress.

Try:

```text
1. Refresh page
2. Wait a few minutes
3. Refresh again
```

---

# 🔄 Update Nextera

If installed through Git:

```bat
cd "%USERPROFILE%\Desktop\Nextera"

git pull
```

Then:

1. Open `chrome://extensions/`
2. Click **Reload** on Nextera
3. Restart Chrome if necessary

---

# 📁 Project Structure

```text
Nextera/
│
├── manifest.json
│
├── native/
│   ├── requirements.txt
│   ├── nextera_native.py
│   ├── nextera_launcher.bat
│   │
│   └── nextera/
│       ├── main.py
│       │
│       └── discussion/
│           └── solver.py
│
├── popup/
│
├── settings/
│
└── README.md
```

> The project structure may change as Nextera evolves.

---

# 🔐 Security

Nextera can potentially handle sensitive local data such as:

* Coursera session cookies
* API keys
* Course content
* Question text
* Generated responses
* Local configuration

### Never commit:

```text
config.json
API keys
Cookies
Session tokens
Personal credentials
```

Use `.gitignore` to protect sensitive files.

Example:

```gitignore
# Secrets
config.json
*.env
.env

# Python
__pycache__/
*.pyc

# Local configuration
.nextera/
```

---

# ⚠️ Disclaimer

Nextera is intended for **educational and personal experimentation**.

The project interacts with Coursera through internal APIs and browser automation mechanisms. Such usage may conflict with Coursera's Terms of Service, course rules, or academic-integrity policies.

By using Nextera, you acknowledge that:

* You are responsible for complying with Coursera's Terms of Service.
* You are responsible for following your institution's academic-integrity policies.
* AI-generated responses can be inaccurate.
* Automated submissions may contain errors.
* The author is not responsible for account restrictions or bans.
* The author is not responsible for incorrect submissions or academic consequences.
* You use the software at your own risk.

**Always review AI-generated content before using or submitting it.**

---

# 👨‍💻 Author

<div align="center">

### Shailendra Meghwal

Computer Science & Engineering

AI & ML

[![GitHub](https://img.shields.io/badge/GitHub-Shailey07-181717?logo=github\&logoColor=white)](https://github.com/shailey07)

</div>

---

# ⭐ Support

If Nextera is useful to you:

```text
⭐ Star the repository
🍴 Fork the project
🐛 Report bugs
💡 Suggest features
```

---

<div align="center">

## ⚡ Nextera

### Automate the workflow. Keep control of the learning.

**Built by [Shailendra Meghwal](https://github.com/shailey07)**

</div>
