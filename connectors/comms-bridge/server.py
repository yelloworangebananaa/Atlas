"""Gmail communication bridge for reading and sending emails using an app password.

Agent-authored MCP server. Review this code, then enable it in the Connectors
panel (it is DISABLED by default).
"""
import os

from mcp.server.fastmcp import FastMCP
import smtplib
import imaplib
import email
from email.mime.text import MIMEText

mcp = FastMCP("comms-bridge")

# Credentials come from the environment: GMAIL_USER + a Gmail App Password
# (https://myaccount.google.com/apppasswords). Never hardcode them.
EMAIL_USER = os.environ.get("GMAIL_USER", "")
EMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

@mcp.tool()
def send_email(to_address: str, subject: str, body: str) -> str:
    """Sends an email via Gmail SMTP.

    Args:
        to_address: The recipient's email address.
        subject: The subject line of the email.
        body: The main content of the email.
    """
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = EMAIL_USER
        msg['To'] = to_address

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, to_address, msg.as_string())
        return f"Email successfully sent to {to_address}"
    except Exception as e:
        return f"Error sending email: {str(e)}"

@mcp.tool()
def read_emails(limit: int = 5) -> str:
    """Reads the latest emails from the inbox.

    Args:
        limit: Number of emails to fetch (default 5).
    """
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select('inbox')
        
        status, data = mail.search(None, 'ALL')
        mail_ids = data[0].split()
        
        results = []
        # Process the most recent ones
        for i in range(max(0, len(mail_ids) - limit), len(mail_ids)):
            status, data = mail.fetch(mail_ids[i], '(RFC822)')
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors='ignore')
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors='ignore')

            results.append({
                'from': msg['From'],
                'subject': msg['Subject'],
                'date': msg['Date'],
                'body': body[:200] + "..." if len(body) > 200 else body
            })
            
        mail.logout()
        import json
        return json.dumps(results, indent=2) if results else "No emails found."
    except Exception as e:
        return f"Error reading emails: {str(e)}"

if __name__ == "__main__":
    mcp.run()
