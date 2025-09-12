import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_SERVER = "smtp.office365.com"   # Or smtp.gmail.com
SMTP_PORT = 587
SMTP_USER = "scanner@roxorgroup.com"
SMTP_PASS = "God32658"  # store in Azure App Service settings!

def send_email(subject, body, to_email):
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_email, msg.as_string())

# Example usage
send_email("Test Subject", "Hello from Azure App Service!", "carlotestr@gmail.com")
