from flask import Flask, g
import threading
import time
import monitor_azure_version  # Your script

app = Flask(__name__)

# Background thread function
def run_invoice_checker_loop():
    while True:
        try:
            print("🔁 Running invoice check...")
            token = monitor_azure_version.get_graph_token()
            if token:
                monitor_azure_version.check_emails(token)
            else:
                print("❌ Failed to acquire token")
        except Exception as e:
            print(f"❌ Error in invoice checker: {e}")
        time.sleep(180)  # Wait 3 minutes

# Start thread before serving (Flask 3+)
@app.before_serving
def start_background_task():
    if not hasattr(g, "invoice_monitor_thread_started"):
        thread = threading.Thread(target=run_invoice_checker_loop, daemon=True)
        thread.start()
        g.invoice_monitor_thread_started = True
        print("✅ Background thread started")

@app.route("/")
def home():
    return "📬 Invoice monitoring app is running every 3 minutes."

if __name__ == "__main__":
    app.run()
