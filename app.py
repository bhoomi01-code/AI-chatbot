import os
import uuid
import json
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, url_for
from google import genai
from google.genai import types
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "exhibition-2026-v1")

# --- STORAGE ---
UPLOAD_FOLDER = os.path.join('static', 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- MONGODB (FRESH NEW DATABASE) ---
mongo_uri = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/")
mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)

# We are using a BRAND NEW DB NAME to avoid confusion with old data
db = mongo_client['exhibition_db_v1'] 
history_collection = db['chat_history']

# Startup Check
try:
    mongo_client.admin.command('ping')
    print("\n" + "="*30)
    print("✨ DATABASE: exhibition_db_v1")
    print(f"📊 CURRENT DOCS: {history_collection.count_documents({})}")
    print("="*30 + "\n")
except Exception as e:
    print(f"❌ DATABASE CONNECTION ERROR: {e}")

# --- GEMINI SETUP ---
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options=types.HttpOptions(api_version="v1"))

def get_active_models():
    try:
        authorized = [m.name.replace('models/', '') for m in client.models.list() if 'generateContent' in m.supported_actions]
        authorized.sort(key=lambda x: ("lite" in x, "pro" in x))
        return authorized if authorized else ["gemini-2.0-flash"]
    except: 
        return ["gemini-2.0-flash", "gemini-2.0-flash-lite"]

MODELS_TO_TRY = get_active_models()

@app.route('/')
def home():
    current_sid = request.args.get('sid')
    # Aggregation for sidebar
    pipeline = [
        {"$sort": {"timestamp": -1}}, 
        {"$group": {"_id": "$session_id", "title": {"$first": "$content"}, "ts": {"$first": "$timestamp"}}}, 
        {"$sort": {"ts": -1}}
    ]
    sessions_list = list(history_collection.aggregate(pipeline))
    messages = list(history_collection.find({"session_id": current_sid}).sort("timestamp", 1)) if current_sid else []
    return render_template('index.html', history=messages, sessions=sessions_list, current_sid=current_sid)

@app.route('/ask', methods=['POST'])
def ask():
    user_msg = request.form.get("message", "")
    image_file = request.files.get("image")
    form_sid = request.form.get("session_id")
    
    # Session Management
    sid = form_sid if form_sid and form_sid.strip() and form_sid != "None" else uuid.uuid4().hex

    # Handle Image
    saved_url = None
    img_bytes, img_mime = None, None
    if image_file:
        ext = os.path.splitext(image_file.filename)[1]
        filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        image_file.save(filepath)
        saved_url = url_for('static', filename=f'uploads/{filename}')
        image_file.seek(0)
        img_bytes = image_file.read()
        img_mime = image_file.mimetype

    # --- THE CRITICAL SAVE (USER MSG) ---
    # We save this immediately before the generator starts.
    try:
        history_collection.insert_one({
            "session_id": sid, 
            "role": "user", 
            "content": user_msg or "Sent Image", 
            "image_url": saved_url, 
            "timestamp": datetime.now()
        })
        print(f"✅ DB UPDATE: User message saved for SID {sid}")
    except Exception as e:
        print(f"🛑 DB ERROR (User Msg): {e}")

    def generate():
        full_reply = ""
        success = False
        
        # Pull context from DB
        past_messages = list(history_collection.find({"session_id": sid}).sort("timestamp", -1).limit(10))
        past_messages.reverse()
        
        chat_context = [
            types.Content(role="user" if m["role"] == "user" else "model", 
            parts=[types.Part(text=m["content"])]) 
            for m in past_messages
        ]
        
        instruction = "[SYSTEM: You are 'my_bot'. Provide clear Markdown responses. Bold key terms.]\n\n"
        if chat_context:
            chat_context[0].parts[0].text = instruction + chat_context[0].parts[0].text

        for model_id in MODELS_TO_TRY:
            if success: break
            try:
                response = client.models.generate_content_stream(
                    model=model_id, 
                    contents=chat_context, 
                    config=types.GenerateContentConfig(temperature=0.7)
                )
                
                for chunk in response:
                    if chunk.text:
                        success = True
                        full_reply += chunk.text
                        yield f"data: {json.dumps({'content': chunk.text, 'session_id': sid})}\n\n"
                
                if success:
                    # --- THE CRITICAL SAVE (BOT MSG) ---
                    history_collection.insert_one({
                        "session_id": sid, 
                        "role": "assistant", 
                        "content": full_reply, 
                        "timestamp": datetime.now()
                    })
                    print(f"✅ DB UPDATE: Assistant reply saved for SID {sid}")
                    break
            except Exception as e:
                print(f"⚠️ API FAIL ({model_id}): {e}")
                continue 

        if not success:
            yield f"data: {json.dumps({'content': '⚠️ *Service busy. Please wait 30 seconds.*', 'session_id': sid})}\n\n"

    return Response(generate(), mimetype='text/event-stream')

@app.route('/delete_session/<sid>', methods=['POST'])
def delete_session(sid):
    history_collection.delete_many({"session_id": sid})
    return jsonify({"status": "ok"})

# Clear Everything (Hidden route for your use only)
@app.route('/wipe_database_danger_zone', methods=['POST'])
def wipe():
    history_collection.delete_many({})
    return "Database Wiped Clean!"

if __name__ == '__main__':
    # Threaded=True is vital for event-stream (streaming)
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)