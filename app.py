from flask import Flask
import threading
import monitor_azure_version  # Your original script

app = Flask(__name__)

def run_invoice_checker():
    token = monitor_azure_version.get_graph_token()
    if token:
        monitor_azure_version.check_emails(token)

@app.before_first_request
def start_background_thread():
    thread = threading.Thread(target=run_invoice_checker)
    thread.start()

@app.route('/')
def home():
    return "📬 Invoice monitoring app is running."

if __name__ == "__main__":
    app.run()
