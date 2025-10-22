# 🔒 Python Project Setup & Execution Guide

This repository demonstrates how to set up a **Python virtual environment**, activate it using **PowerShell**, and run a Python script (`secret.py`). This setup ensures a clean, isolated environment for your project and prevents dependency conflicts.

---

## 📋 Prerequisites

Before you start, make sure you have:

- **Python 3.10 or higher** installed on your system. You can check your version:

```powershell
python --version

⚙️ Step 1: Clone the Repository

First, clone this repository (or navigate to your project folder):

git clone https://github.com/your-username/your-repo.git
cd your-repo

git clone https://github.com/your-username/your-repo.git
cd your-repo

🧩 Step 2: Create a Virtual Environment

Create a virtual environment named venv:

python -m venv venv

🚀 Step 3: Activate the Virtual Environment

Activate the virtual environment using PowerShell:

.\venv\Scripts\Activate.ps1

⚠️ Note: If you get an error about script execution, run:

Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser


Then try activating again.

When the environment is activated, your terminal prompt will change, showing (venv) at the beginning.

📦 Step 4: Install Dependencies

If your project has dependencies, install them using pip:

pip install -r requirements.txt


Tip: If you install new packages, update the requirements.txt:

pip freeze > requirements.txt

🧪 Step 5: Run Your Python Script

Run the main Python file (secret.py) within the activated environment:

python secret.py


This will execute the script in an isolated environment, preventing conflicts with your global Python installation.

🧹 Step 6: Deactivate the Environment

Once you’re done working:

deactivate


Your terminal prompt will return to normal, and the virtual environment will be inactive.

🗂️ Recommended Project Structure
project-folder/
│
├── venv/                  # Virtual environment
├── secret.py              # Main Python script
├── requirements.txt       # List of dependencies
└── README.md              # Project instructions

🔑 Example secret.py


