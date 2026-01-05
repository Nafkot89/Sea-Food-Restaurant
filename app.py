from tenacity import retry, stop_after_attempt, wait_exponential
import os
import json
from datetime import datetime
from flask import Flask, request, render_template_string, jsonify
import google.generativeai as genai
import gspread
from google.oauth2 import service_account

app = Flask(__name__)

# ========== CONFIGURATION ==========
GEMINI_API_KEY = os.environ.get("GOOGLE_AI_API_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# ========== AI PROMPT ==========
SYSTEM_PROMPT = """
You are a precise financial data parser. Extract transactions from the bank statement PDF.
EXTRACT FROM THESE SECTIONS ONLY:
- DEPOSITS AND ADDITIONS
- CHECKS PAID
- ATM & DEBIT CARD WITHDRAWALS
- ELECTRONIC WITHDRAWALS
- OTHER WITHDRAWALS
- FEES
RULES:
- Date: Capture date (e.g., 05/01)
- Description: Extract ONLY text on the same line as the AMOUNT
- Account: Leave as empty string ""
- Amount: Capture numerical amount as shown
OUTPUT: JSON array with objects: date, description, account, amount
Example: [{"date": "05/01", "description": "Orig CO Name: Uber USA 6787", "account": "", "amount": "505.01"}]
"""

# ========== RETRY HELPER ==========
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def call_gemini_with_retry(model, contents):
    """Calls the Gemini API with automatic retries on failure."""
    return model.generate_content(contents)

# ========== HTML PAGE ==========
HTML_PAGE = """<!DOCTYPE html>
<html><head><title>SnowCrab Restaurant - Statement Upload</title>
<style>body{font-family:Arial; max-width:600px; margin:40px auto; padding:20px;}
.container{border:2px dashed #ccc; padding:30px; text-align:center; border-radius:10px;}
input[type="file"]{margin:20px 0;}
button{background:#f97316; color:white; border:none; padding:12px 24px; border-radius:6px; cursor:pointer;}
.message{margin-top:20px; padding:10px; border-radius:5px;}
.success{background:#d1fae5; color:#065f46;}
.error{background:#fee2e2; color:#991b1b;}</style></head>
<body><div class="container"><h2>Upload Monthly Bank Statement</h2>
<p>Upload your Chase PDF statement for processing</p>
<form id="uploadForm"><input type="file" id="pdfFile" name="file" accept=".pdf" required><br><br>
<button type="submit">Process Statement</button></form>
<div id="message" class="message" style="display:none;"></div>
<div id="loading" style="display:none;">Processing with AI... Please wait.</div></div>
<script>document.getElementById('uploadForm').addEventListener('submit', async (e) => {
e.preventDefault(); const fileInput = document.getElementById('pdfFile'); const file = fileInput.files[0];
if (!file) return; document.getElementById('loading').style.display = 'block';
document.getElementById('message').style.display = 'none'; const formData = new FormData();
formData.append('file', file); try { const response = await fetch('/upload', { method: 'POST', body: formData });
const result = await response.json(); document.getElementById('loading').style.display = 'none';
const messageDiv = document.getElementById('message'); messageDiv.style.display = 'block';
if (result.success) { messageDiv.className = 'message success'; messageDiv.innerHTML = '<strong>Success!</strong><br>Processed ' + result.transaction_count + ' transactions.<br>Data saved to Google Sheets Queue.'; } 
else { messageDiv.className = 'message error'; messageDiv.innerHTML = '<strong>Error:</strong> ' + result.error; } } 
catch (error) { document.getElementById('loading').style.display = 'none'; const messageDiv = document.getElementById('message');
messageDiv.style.display = 'block'; messageDiv.className = 'message error'; messageDiv.innerHTML = '<strong>Network Error:</strong> Please try again.'; } });</script>
</body></html>"""

@app.route('/')
def index():
    return render_template_string(HTML_PAGE)

@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        # 1. Check configuration
        if not GEMINI_API_KEY: return jsonify({"success": False, "error": "Gemini API key not configured"})
        if not SPREADSHEET_ID: return jsonify({"success": False, "error": "Spreadsheet ID not configured"})
        if not SERVICE_ACCOUNT_JSON: return jsonify({"success": False, "error": "Google Service Account not configured"})
        
        # 2. Get uploaded file
        if 'file' not in request.files: return jsonify({"success": False, "error": "No file uploaded"})
        file = request.files['file']
        if file.filename == '': return jsonify({"success": False, "error": "No file selected"})
        
        # 3. Configure Gemini AI
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.5-pro')
        
        # 4. Extract text from PDF (WITH RETRY)
        pdf_bytes = file.read()
        response = call_gemini_with_retry(model, [ SYSTEM_PROMPT, {"mime_type": "application/pdf", "data": pdf_bytes} ])
        
        # 5. Parse JSON response
        json_text = response.text.strip()
        if json_text.startswith('```json'): json_text = json_text[7:-3]  # Remove markdown formatting
        transactions = json.loads(json_text)
        
        # 6. Connect to Google Sheets
        creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(creds_dict, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        gc = gspread.authorize(credentials)
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        
        # 7. Get or create Queue worksheet
        try: worksheet = spreadsheet.worksheet("Queue")
        except: worksheet = spreadsheet.add_worksheet(title="Queue", rows="1000", cols="3")
        worksheet.update('A1:C1', [['Timestamp', 'Statement_Month', 'Transaction_JSON']])
        
        # 8. Prepare and append data
        statement_month = datetime.now().strftime("%B %Y")
        timestamp = datetime.utcnow().isoformat() + "Z"
        new_row = [ timestamp, statement_month, json.dumps(transactions) ]
        worksheet.append_row(new_row)
        
        return jsonify({"success": True, "transaction_count": len(transactions), "message": f"Added {len(transactions)} transactions to Queue"})
        
    except Exception as e:
        return jsonify({"success": False, "error": f"Processing failed: {str(e)}"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
