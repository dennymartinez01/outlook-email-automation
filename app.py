from flask import Flask
import threading
import monitor_azure_version  # Your original script

app = Flask(__name__)

# Flag to ensure the background thread only starts once
app.invoice_thread_started = False

def run_invoice_checker():
    token = monitor_azure_version.get_graph_token()
    if token:
        monitor_azure_version.check_emails(token)

@app.before_request
def start_background_thread():
    if not app.invoice_thread_started:
        thread = threading.Thread(target=run_invoice_checker, daemon=True)
        thread.start()
        app.invoice_thread_started = True

@app.route('/')
def home():
    return "📬 Invoice monitoring app is running."

if __name__ == "__main__":
    app.run()
