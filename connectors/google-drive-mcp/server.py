"""Google Drive MCP: search and read files (read-only scope).

Enabled by `python setup.py` (Google Drive step), which installs the Google
libraries into the venv, copies your OAuth client JSON here, and runs
`python server.py auth` for the one-time browser sign-in. Tokens refresh
themselves after that.
"""
import json
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("google-drive-mcp")

DIR = Path(__file__).resolve().parent  # creds/token live next to this file, not the CWD
CREDS_PATH = DIR / "google_drive_creds.json"
TOKEN_PATH = DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
NOT_AUTHED = "Not authorized yet — run `python setup.py` (Google Drive step) to sign in."


def get_drive_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN_PATH.exists():
        return NOT_AUTHED
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        else:
            return NOT_AUTHED
    return build("drive", "v3", credentials=creds)


@mcp.tool()
def search_files(q: str) -> str:
    """Search for files in Google Drive.

    Args:
        q: The search query (Drive query syntax, e.g. "name contains 'report'").
    """
    try:
        service = get_drive_service()
        if isinstance(service, str):
            return service
        results = service.files().list(q=q, pageSize=10, fields="nextPageToken, files(id, name)").execute()
        items = results.get("files", [])
        return json.dumps(items, indent=2) if items else "No files found."
    except Exception as e:
        return f"Error searching files: {str(e)}"


@mcp.tool()
def read_file(file_id: str) -> str:
    """Read a text-based file from Google Drive given its ID.

    Args:
        file_id: The ID of the file to read.
    """
    try:
        service = get_drive_service()
        if isinstance(service, str):
            return service
        content = service.files().export(fileId=file_id, mimeType="text/plain").execute()
        if isinstance(content, bytes):
            return content.decode("utf-8")
        return str(content)
    except Exception as e:
        return f"Error reading file: {str(e)}"


def auth():
    """One-time interactive OAuth (run by setup.py): browser sign-in, saves token.json."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CREDS_PATH.exists():
        sys.exit(f"Missing {CREDS_PATH} — download an OAuth client JSON first (python setup.py walks you through it).")
    creds = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES).run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"Authorized — token saved to {TOKEN_PATH}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        auth()
    else:
        mcp.run()
