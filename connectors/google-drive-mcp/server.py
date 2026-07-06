"""Google Drive MCP for managing files, searching, and reading documents.

Agent-authored MCP server. Review this code, then enable it in the Connectors
panel (it is DISABLED by default).
"""
from mcp.server.fastmcp import FastMCP
import os
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

mcp = FastMCP("google-drive-mcp")

CREDS_PATH = "google_drive_creds.json"
TOKEN_PATH = "token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            return "Auth required: Please run the manual authentication flow first."

        with open(TOKEN_PATH, "w") as token:
            token.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


@mcp.tool()
def search_files(q: str) -> str:
    """Search for files in Google Drive.

    Args:
        q: The search query.
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


if __name__ == "__main__":
    mcp.run()
