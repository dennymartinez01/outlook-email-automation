from flask import Flask
import threading
import time
import monitor_azure_version

app = Flask(__name__)
app.invoice_thread_started = False

def run_invoice_checker_loop():
    while True:
        try:
            print("🔁 Running invoice check...")
            token = monitor_azure_version.get_graph_token()
            if token:
                monitor_azure_version.check_emails(token)
            else:
                print("❌ Failed to get token")
        except Exception as e:
            print(f"❌ Error during invoice check: {e}")
        time.sleep(180)  # Run every 3 minutes

@app.before_first_request
def start_background_thread():
    if not app.invoice_thread_started:
        thread = threading.Thread(target=run_invoice_checker_loop, daemon=True)
        thread.start()
        app.invoice_thread_started = True
        print("✅ Background thread started")

@app.route('/')
def home():
    return "📬 Invoice monitoring app is running every 3 minutes."

if __name__ == "__main__":
    app.run()