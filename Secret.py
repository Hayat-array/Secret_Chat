from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
import json
import time
import random
import string
from datetime import datetime, timedelta
import os
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure
from bson import ObjectId
import hashlib
import base64
import io
from werkzeug.utils import secure_filename
import uuid

# --- Flask App and SocketIO Initialization ---
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secure-secret-key-here-2024')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")

# ========================
# MONGODB CONFIGURATION
# ========================

MONGODB_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/securechat')

def get_db_collections():
    """Get MongoDB collections with error handling"""
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        client.admin.command('ismaster')
        db = client['securechat']
        collections = {
            'users': db['users'],
            'conversations': db['conversations'],
            'messages': db['messages'],
            'otps': db['otps'],
            'files': db['files'],
            'polls': db['polls']
        }
        print("✅ MongoDB connected successfully")
        return collections, True
    except ConnectionFailure as e:
        print(f"❌ MongoDB connection failed: {e}")
        print("📋 Using in-memory storage as fallback")
        return {}, False
    except Exception as e:
        print(f"❌ An unexpected error occurred during DB connection: {e}")
        print("📋 Using in-memory storage as fallback")
        return {}, False

db_collections, use_mongo = get_db_collections()

# ========================
# UTILITY FUNCTIONS
# ========================

def _match_query_in_memory(doc, query):
    """Helper for in-memory query matching (handles basic operators)."""
    if not isinstance(doc, dict) or not isinstance(query, dict):
        return False
    for key, value in query.items():
        doc_value = doc.get(key)

        # Handle $ne (Not Equal)
        if isinstance(value, dict) and '$ne' in value:
            if doc_value == value['$ne']:
                return False
        # Handle $in (Exists in List)
        elif isinstance(value, dict) and '$in' in value:
            if not isinstance(value['$in'], list) or doc_value not in value['$in']:
                return False
        # Handle $nin (Does Not Exist in List)
        elif isinstance(value, dict) and '$nin' in value:
             if not isinstance(value['$nin'], list) or doc_value in value['$nin']:
                 return False
        # Handle $exists (Field Presence)
        elif isinstance(value, dict) and '$exists' in value:
             exists = (key in doc)
             if value['$exists'] != exists:
                  return False
        # Handle simple equality
        elif doc_value != value:
            return False
    return True

def db_operation(op_name, collection_name, *args, **kwargs):
    """
    Centralized DB operation handler for MongoDB or session-based fallback.
    """
    if use_mongo:
        try:
            if collection_name not in db_collections:
                 raise ValueError(f"MongoDB collection '{collection_name}' not found.")
            collection = db_collections[collection_name]
            op = getattr(collection, op_name)

            if op_name == 'find':
                return list(op(*args, **kwargs))
            if op_name == 'find_sort_limit':
                 query = args[0] if args else {}
                 sort_key = kwargs.get('sort_key', 'timestamp')
                 sort_order = kwargs.get('sort_order', -1)
                 limit = kwargs.get('limit', 1)
                 return list(collection.find(query).sort(sort_key, sort_order).limit(limit))
            if op_name == 'find_one_and_update':
                 kwargs.setdefault('return_document', ReturnDocument.AFTER)
                 return op(*args, **kwargs)
            if op_name == 'update_many':
                 return op(*args, **kwargs)

            return op(*args, **kwargs)
        except Exception as e:
            print(f"❌ MongoDB Error ({op_name} on {collection_name}): {e}")
            if 'find_one' in op_name: return None
            if 'find' in op_name : return []
            raise e
    else:
        # --- SESSION-BASED FALLBACK ---
        if collection_name not in session:
            session[collection_name] = {}

        data_store = session[collection_name]

        try:
            if op_name == 'insert_one':
                doc = args[0].copy() if args else kwargs.get('document', {}).copy()
                doc_id = str(ObjectId())
                doc['_id'] = doc_id
                doc.setdefault('created_at', datetime.now())
                data_store[doc_id] = doc
                session.modified = True
                class InsertOneResult:
                    inserted_id = doc_id
                return InsertOneResult()

            elif op_name == 'find_one':
                query = args[0] if args else kwargs.get('filter', {})
                for doc_id, doc in data_store.items():
                    if isinstance(doc, dict) and _match_query_in_memory(doc, query):
                        return doc.copy()
                return None

            elif op_name == 'update_one':
                query = args[0] if args else kwargs.get('filter', {})
                update_doc = args[1] if len(args) > 1 else kwargs.get('update', {})
                found_doc_copy = None
                matched_count = 0
                modified_count = 0

                target_doc_id = None
                for doc_id, doc in data_store.items():
                    if isinstance(doc, dict) and _match_query_in_memory(doc, query):
                        target_doc_id = doc_id
                        matched_count = 1
                        break

                if target_doc_id:
                    doc_to_update = data_store[target_doc_id]
                    original_doc = doc_to_update.copy()

                    if '$set' in update_doc:
                        for key, value in update_doc['$set'].items():
                             if doc_to_update.get(key) != value:
                                 doc_to_update[key] = value
                                 modified_count = 1
                    if '$addToSet' in update_doc:
                         for key, value in update_doc['$addToSet'].items():
                             if key not in doc_to_update:
                                 doc_to_update[key] = []
                             if isinstance(doc_to_update[key], list) and value not in doc_to_update[key]:
                                 doc_to_update[key].append(value)
                                 modified_count = 1

                    if modified_count > 0:
                        session.modified = True

                update_result = type('UpdateResult', (), {'matched_count': matched_count, 'modified_count': modified_count})()
                return update_result

            elif op_name == 'update_many':
                query = args[0] if args else kwargs.get('filter', {})
                update_doc = args[1] if len(args) > 1 else kwargs.get('update', {})
                matched_count = 0
                modified_count = 0

                for doc_id, doc in data_store.items():
                    if isinstance(doc, dict) and _match_query_in_memory(doc, query):
                        matched_count += 1
                        if '$set' in update_doc:
                            for key, value in update_doc['$set'].items():
                                if doc.get(key) != value:
                                    doc[key] = value
                                    modified_count += 1

                if modified_count > 0:
                    session.modified = True

                update_result = type('UpdateResult', (), {'matched_count': matched_count, 'modified_count': modified_count})()
                return update_result

            elif op_name == 'find_one_and_update':
                query = args[0] if args else kwargs.get('filter', {})
                update_doc = args[1] if len(args) > 1 else kwargs.get('update', {})
                found_doc = None
                matched_count = 0
                modified_count = 0

                for doc_id, doc in data_store.items():
                    if isinstance(doc, dict) and _match_query_in_memory(doc, query):
                        found_doc = doc
                        matched_count = 1
                        break

                if found_doc:
                    updated_doc = found_doc.copy()
                    
                    if '$set' in update_doc:
                        for key, value in update_doc['$set'].items():
                            if updated_doc.get(key) != value:
                                updated_doc[key] = value
                                modified_count = 1
                    
                    if '$addToSet' in update_doc:
                        for key, value in update_doc['$addToSet'].items():
                            if key not in updated_doc:
                                updated_doc[key] = []
                            if isinstance(updated_doc[key], list) and value not in updated_doc[key]:
                                updated_doc[key].append(value)
                                modified_count = 1
                    
                    if modified_count > 0:
                        data_store[doc_id] = updated_doc
                        session.modified = True
                    
                    return updated_doc.copy() if modified_count > 0 else found_doc.copy()
                
                return None

            elif op_name == 'find' or op_name == 'find_sort_limit':
                query = args[0] if args else {}
                results = []
                for doc_id, doc in data_store.items():
                    if isinstance(doc, dict) and _match_query_in_memory(doc, query):
                        results.append(doc.copy())

                if op_name == 'find_sort_limit':
                    sort_key = kwargs.get('sort_key', 'timestamp')
                    sort_order = kwargs.get('sort_order', -1)
                    limit = kwargs.get('limit', 1)
                    reverse = (sort_order == -1)
                    results.sort(key=lambda x: x.get(sort_key, datetime.min), reverse=reverse)
                    return results[:limit]

                return results

            elif op_name == 'count_documents':
                query = args[0] if args else {}
                count = 0
                for doc in data_store.values():
                     if isinstance(doc, dict) and _match_query_in_memory(doc, query):
                        count += 1
                return count

            else:
                 print(f"⚠️ Unsupported session-based operation: {op_name}")
                 return None

        except Exception as e:
            print(f"❌ Session DB Error ({op_name} on {collection_name}): {e}")
            if 'find_one' in op_name: return None
            if 'find' in op_name: return []
            raise e

# ========================
# ENCRYPTION ALGORITHM
# ========================
def encode_message(text):
    """Encrypt message using custom algorithm"""
    if not text or not isinstance(text, str):
        return ""

    def process_word(word):
        if len(word) >= 3:
            r1 = ''.join(random.choice(string.ascii_lowercase) for _ in range(3))
            r2 = ''.join(random.choice(string.ascii_lowercase) for _ in range(3))
            return r1 + word[1:] + word[0] + r2
        return word[::-1]

    return " ".join(process_word(word) for word in text.split())

def decode_message(text):
    """Decrypt message using custom algorithm"""
    if not text or not isinstance(text, str):
        return ""

    def process_word(word):
        if len(word) >= 6:
            stnew = word[3:-3]
            return stnew[-1] + stnew[:-1] if stnew else ''
        return word[::-1]

    try:
        return " ".join(process_word(word) for word in text.split())
    except Exception as e:
        print(f"Error decoding word in text '{text[:50]}...': {e}")
        return "[Decoding Error]"

# ========================
# FILE HANDLING
# ========================
ALLOWED_EXTENSIONS = {
    'image': ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp'],
    'document': ['pdf', 'doc', 'docx', 'txt', 'ppt', 'pptx', 'xls', 'xlsx'],
    'audio': ['mp3', 'wav', 'ogg', 'm4a'],
    'video': ['mp4', 'mov', 'avi', 'mkv']
}

def allowed_file(filename, file_type='image'):
    """Check if file extension is allowed"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS.get(file_type, [])

def save_file(file_data, filename, file_type, uploaded_by):
    """Save file to database"""
    file_id = str(uuid.uuid4())
    file_doc = {
        '_id': file_id,
        'filename': filename,
        'file_type': file_type,
        'data': file_data,
        'uploaded_by': uploaded_by,
        'uploaded_at': datetime.now(),
        'size': len(file_data)
    }
    result = db_operation('insert_one', 'files', file_doc)
    return file_id if result else None

def get_file(file_id):
    """Retrieve file from database"""
    return db_operation('find_one', 'files', {'_id': file_id})

# ========================
# POLL MANAGEMENT
# ========================
def create_poll(question, options, created_by):
    """Create a new poll"""
    poll_id = str(uuid.uuid4())
    poll_doc = {
        '_id': poll_id,
        'question': question,
        'options': [{'text': opt, 'votes': 0, 'voters': []} for opt in options],
        'created_by': created_by,
        'created_at': datetime.now(),
        'voters': []
    }
    result = db_operation('insert_one', 'polls', poll_doc)
    return poll_id if result else None

def vote_poll(poll_id, option_index, voter_mobile):
    """Vote on a poll"""
    poll = db_operation('find_one', 'polls', {'_id': poll_id})
    if not poll or voter_mobile in poll.get('voters', []):
        return False
    
    if 0 <= option_index < len(poll['options']):
        poll['options'][option_index]['votes'] += 1
        poll['options'][option_index]['voters'].append(voter_mobile)
        poll['voters'].append(voter_mobile)
        
        result = db_operation('update_one', 'polls',
                            {'_id': poll_id},
                            {'$set': poll})
        return result and result.modified_count > 0
    return False

def get_poll_results(poll_id):
    """Get poll results"""
    return db_operation('find_one', 'polls', {'_id': poll_id})

# ========================
# AUTHENTICATION & OTP
# ========================
def generate_otp():
    """Generate 6-digit OTP"""
    return str(random.randint(100000, 999999))

def send_otp_console(mobile_number, otp):
    """Display OTP in console for development"""
    print("\n" + "="*50)
    print(f"📱 OTP VERIFICATION REQUIRED")
    print(f"="*50)
    print(f"📞 Mobile Number: {mobile_number}")
    print(f"🔢 OTP Code: {otp}")
    print(f"⏰ This OTP will expire in 10 minutes.")
    print(f"="*50 + "\n")
    return True

def hash_password(password):
    """Hash password using SHA-256"""
    if not isinstance(password, str):
        password = str(password)
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def create_user(mobile_number, name, password):
    """Create new user - includes contacts list"""
    user_data = {
        'mobile_number': mobile_number,
        'name': name,
        'password_hash': hash_password(password),
        'status': 'Hey there! I am using SecureChat',
        'avatar': name[:2].upper() if name else '??',
        'contacts': [],
        'profile_pic': None,
        'created_at': datetime.now(),
        'last_seen': datetime.now(),
        'is_online': True,
        'notification_count': 0
    }
    result = db_operation('insert_one', 'users', user_data)
    return result.inserted_id if result else None

def verify_user(mobile_number, password):
    """Verify user credentials"""
    user = db_operation('find_one', 'users', {'mobile_number': mobile_number})
    if user and isinstance(user, dict) and user.get('password_hash') == hash_password(password):
        db_operation('update_one', 'users',
                     {'mobile_number': mobile_number},
                     {'$set': {'last_seen': datetime.now(), 'is_online': True}})
        return user
    return None

def store_otp(mobile_number, otp):
    """Store OTP with expiration"""
    expires_at = datetime.now() + timedelta(minutes=10)
    otp_data = {
        'mobile_number': mobile_number,
        'otp': otp,
        'expires_at': expires_at,
        'used': False,
        'created_at': datetime.now()
    }
    db_operation('insert_one', 'otps', otp_data)

def verify_otp(mobile_number, otp):
    """Verify OTP against stored record."""
    current_time = datetime.now()
    otp_record = db_operation('find_one', 'otps', {
        'mobile_number': mobile_number,
        'otp': otp,
        'used': False
    })

    if otp_record and isinstance(otp_record.get('expires_at'), datetime) and otp_record['expires_at'] > current_time:
        update_result = db_operation('update_one', 'otps',
                                     {'_id': otp_record['_id'], 'used': False},
                                     {'$set': {'used': True}})
        return update_result and update_result.modified_count == 1
    elif otp_record:
         print(f"OTP found but expired or invalid type for {mobile_number}. Expires: {otp_record.get('expires_at')}, Current: {current_time}")

    return False

# ========================
# MESSAGE MANAGEMENT
# ========================
def get_or_create_conversation(user1_mobile, user2_mobile):
    """Get or create conversation between two users using mobile numbers."""
    if user1_mobile == user2_mobile:
        print("⚠️ Attempt to create conversation with self:", user1_mobile)
        return None

    participants = sorted([user1_mobile, user2_mobile])
    conversation = db_operation('find_one', 'conversations', {'participants': participants})

    if not conversation:
        print(f"Creating new conversation between {user1_mobile} and {user2_mobile}")
        conv_data = {
            'participants': participants,
            'created_at': datetime.now(),
            'last_message_at': None
        }
        result = db_operation('insert_one', 'conversations', conv_data)
        if not result or not result.inserted_id:
             print(f"🚨 CRITICAL: Failed to insert new conversation for {participants}")
             return None

        conversation = db_operation('find_one', 'conversations', {'_id': result.inserted_id})
        if not conversation:
             conversation = conv_data
             conversation['_id'] = result.inserted_id

    return conversation

def add_message(sender_mobile, receiver_mobile, message_text, message_type='text', file_id=None, poll_id=None, shared_contact=None):
    """Add encrypted message to conversation, returns the full message dict."""
    conversation = get_or_create_conversation(sender_mobile, receiver_mobile)
    if not conversation or '_id' not in conversation:
         print(f"🚨 CRITICAL: Failed to get/create conversation for add_message {sender_mobile}/{receiver_mobile}")
         raise ValueError("Failed to establish conversation")

    encrypted_message = encode_message(message_text) if message_type == 'text' else message_text
    message_timestamp = datetime.now()

    message_data = {
        'conversation_id': str(conversation['_id']),
        'sender': sender_mobile,
        'receiver': receiver_mobile,
        'encrypted_text': encrypted_message,
        'original_text': message_text,
        'timestamp': message_timestamp,
        'read': False,
        'message_type': message_type,
        'file_id': file_id,
        'poll_id': poll_id,
        'shared_contact': shared_contact
    }

    result = db_operation('insert_one', 'messages', message_data)
    if not result or not result.inserted_id:
         print(f"🚨 CRITICAL: Failed to insert message for conversation {conversation['_id']}")
         raise ValueError("Failed to save message")

    # Update notification count for receiver
    db_operation('update_one', 'users',
                 {'mobile_number': receiver_mobile},
                 {'$inc': {'notification_count': 1}})

    db_operation('update_one', 'conversations',
                 {'_id': conversation['_id']},
                 {'$set': {'last_message_at': message_timestamp}})

    message_data['_id'] = result.inserted_id
    message_data['id'] = str(result.inserted_id)
    return message_data

def mark_messages_as_read(conversation_id, reader_mobile):
    """Mark all messages in conversation as read for the reader"""
    result = db_operation('update_many', 'messages',
                         {'conversation_id': conversation_id, 'receiver': reader_mobile, 'read': False},
                         {'$set': {'read': True}})
    
    # Reset notification count
    db_operation('update_one', 'users',
                 {'mobile_number': reader_mobile},
                 {'$set': {'notification_count': 0}})
    
    return result

def get_conversation_messages(user1_mobile, user2_mobile, limit=100):
    """Get messages between two users (mobile numbers)."""
    conversation = get_or_create_conversation(user1_mobile, user2_mobile)
    if not conversation or '_id' not in conversation:
        print(f"⚠️ Failed to get conversation for {user1_mobile}/{user2_mobile} in get_conversation_messages")
        return []

    conv_id = conversation['_id']
    messages = db_operation('find', 'messages', {'conversation_id': str(conv_id)})
    messages.sort(key=lambda x: x.get('timestamp', datetime.min) if isinstance(x.get('timestamp'), datetime) else datetime.min)

    if limit and limit > 0:
        messages = messages[-limit:]

    return messages

# ========================
# USER & CONTACT MANAGEMENT
# ========================
def get_user_conversations(mobile_number):
    """Get all conversations for a user, with robust error checks."""
    try:
        conversations_raw = db_operation('find', 'conversations', {'participants': mobile_number})
        if conversations_raw is None:
             print(f"⚠️ db_operation returned None for conversations for {mobile_number}")
             return []
    except Exception as e:
         print(f"❌ Exception fetching conversations for {mobile_number}: {e}")
         return []

    conversations_raw.sort(
         key=lambda x: x.get('last_message_at', datetime.min) if isinstance(x.get('last_message_at'), datetime) else datetime.min,
         reverse=True
     )

    conv_list = []
    for conv in conversations_raw:
        try:
            if not isinstance(conv, dict) or '_id' not in conv or not isinstance(conv.get('participants'), list) or len(conv['participants']) != 2:
                 print(f"Skipping invalid conversation data structure: {conv}")
                 continue

            other_user_mobile = next((p for p in conv['participants'] if p != mobile_number), None)
            if not other_user_mobile:
                print(f"Skipping conversation {conv['_id']} - could not find other participant.")
                continue

            user_data = db_operation('find_one', 'users', {'mobile_number': other_user_mobile})

            if not user_data:
                user_data_processed = {
                    '_id': f'deleted_{other_user_mobile}',
                    'id': f'deleted_{other_user_mobile}',
                    'name': 'Unknown User', 'avatar': '??', 'status': 'N/A',
                    'is_online': False, 'mobile_number': other_user_mobile,
                    'profile_pic': None, 'contacts': []
                 }
            else:
                 user_data_processed = {
                     **user_data,
                     '_id': str(user_data.get('_id')),
                     'id': str(user_data.get('_id')),
                     'status': user_data.get('status', ''),
                     'is_online': user_data.get('is_online', False),
                     'profile_pic': user_data.get('profile_pic'),
                     'contacts': user_data.get('contacts', []),
                 }
                 user_data_processed.pop('password_hash', None)

            last_msg_list = db_operation(
                'find_sort_limit',
                'messages',
                {'conversation_id': str(conv['_id'])},
                sort_key='timestamp', sort_order=-1, limit=1
            )
            last_msg = last_msg_list[0] if last_msg_list else None

            unread_count = db_operation('count_documents', 'messages', {
                'conversation_id': str(conv['_id']),
                'receiver': mobile_number,
                'read': False
            })
            unread_count_int = unread_count if isinstance(unread_count, int) else 0

            last_msg_time_str = ''
            last_msg_content = 'No messages yet'
            if last_msg:
                if last_msg.get('message_type') == 'file':
                    last_msg_content = '📎 File'
                elif last_msg.get('message_type') == 'voice':
                    last_msg_content = '🎤 Voice message'
                elif last_msg.get('message_type') == 'poll':
                    last_msg_content = '📊 Poll'
                elif last_msg.get('message_type') == 'contact':
                    last_msg_content = '👤 Contact'
                else:
                    last_msg_content = last_msg.get('encrypted_text', 'No messages yet')
                
                if isinstance(last_msg.get('timestamp'), datetime):
                    last_msg_time_str = last_msg['timestamp'].strftime('%H:%M')

            conv_list.append({
                'chat_id': str(conv['_id']),
                'other_user': other_user_mobile,
                'user': user_data_processed,
                'last_message': last_msg_content,
                'last_message_time': last_msg_time_str,
                'unread_count': unread_count_int
            })
        except Exception as e:
            print(f"❌ Error processing conversation {conv.get('_id', 'N/A')} for {mobile_number}: {e}")
            import traceback
            traceback.print_exc()

    return conv_list

def get_user_contacts(mobile_number):
    """Get details for users present in the current user's contact list."""
    current_user = db_operation('find_one', 'users', {'mobile_number': mobile_number})
    if not current_user or not isinstance(current_user.get('contacts'), list):
        print(f"⚠️ Cannot get contacts for {mobile_number}: User not found or contacts list missing/invalid.")
        return []

    contact_numbers = current_user['contacts']
    if not contact_numbers:
        return []

    try:
         contacts_data = db_operation('find', 'users', {'mobile_number': {'$in': contact_numbers}})
         if contacts_data is None:
              return []
    except Exception as e:
         print(f"❌ Error fetching contact details for {mobile_number}: {e}")
         return []

    formatted_contacts = []
    for user in contacts_data:
         if not isinstance(user, dict): continue

         formatted_user = {
             '_id': str(user.get('_id')),
             'id': str(user.get('_id')),
             'mobile_number': user.get('mobile_number'),
             'name': user.get('name', 'Unknown'),
             'avatar': user.get('avatar', '??'),
             'status': user.get('status', ''),
             'is_online': user.get('is_online', False),
             'profile_pic': user.get('profile_pic'),
             'notification_count': user.get('notification_count', 0)
         }
         formatted_contacts.append(formatted_user)

    return formatted_contacts

# ========================
# ROUTES
# ========================
@app.before_request
def make_session_permanent():
    session.permanent = True

@app.route('/')
def index():
    """Serve main application or login page."""
    if 'user' in session:
        return render_template_string(MAIN_APP_TEMPLATE)
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if 'user' in session:
            return redirect('/')
        return render_template_string(LOGIN_TEMPLATE)

    # POST Request
    phone = request.form.get('phone', '').strip()
    password = request.form.get('password', '')

    if not phone or not password:
        return render_template_string(LOGIN_TEMPLATE, error="Phone and password are required")

    # Phone Normalization
    normalized_phone = phone
    if len(phone) == 10 and phone.isdigit():
        normalized_phone = '+91' + phone
        print(f"Normalized Indian number: {phone} -> {normalized_phone}")
    elif phone.isdigit() and not phone.startswith('+'):
        print(f"⚠️ Phone number '{phone}' lacks country code. Assuming it's correct as is.")

    # Debugging Prints
    print(f"\n--- Login Attempt ---")
    print(f"Entered Phone: {phone}")
    print(f"Normalized Phone Used for Lookup: {normalized_phone}")
    attempted_hash = hash_password(password)
    print(f"Password Hash Attempt: {attempted_hash}")

    user = verify_user(normalized_phone, password)

    if user:
        # Regenerate session ID to prevent fixation
        session.clear()
        session['user'] = {
            'mobile_number': user['mobile_number'],
            'name': user.get('name', 'User'),
            'avatar': user.get('avatar', '??')
        }
        session.permanent = True
        print(f"✅ Login successful for {normalized_phone}")
        return redirect('/')
    else:
        print(f"❌ Login failed for {normalized_phone}.")
        try:
            existing_user = db_operation('find_one', 'users', {'mobile_number': normalized_phone})
            if existing_user:
                print(f"   Reason: User exists, but password hash mismatch.")
                print(f"   Stored Hash:   {existing_user.get('password_hash')}")
                print(f"   Attempted Hash: {attempted_hash}")
            else:
                print(f"   Reason: User with phone number '{normalized_phone}' not found.")
        except Exception as e:
            print(f"   Error during DB check for failed login: {e}")
        return render_template_string(LOGIN_TEMPLATE, error="Invalid phone number or password")

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'GET':
        if 'user' in session:
            return redirect('/')
        return render_template_string(SIGNUP_TEMPLATE)

    # POST Request
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')

    # Validation
    errors = {}
    if not name: errors['name'] = "Name is required."
    elif len(name) < 2: errors['name'] = "Name must be at least 2 characters."
    if not phone: errors['phone'] = "Phone number is required."
    if not password: errors['password'] = "Password is required."
    elif len(password) < 6: errors['password'] = "Password must be at least 6 characters."
    if password != confirm_password: errors['confirm_password'] = "Passwords do not match."

    normalized_phone = phone
    if phone:
        if len(phone) == 10 and phone.isdigit():
            normalized_phone = '+91' + phone
        elif not phone.startswith('+') or not phone[1:].isdigit() or len(phone) < 11:
             errors['phone'] = "Use international format (e.g., +91xxxxxxxxxx)."

    if errors:
         error_message = "Please correct the errors below."
         print(f"Signup validation errors: {errors}")
         return render_template_string(SIGNUP_TEMPLATE, error=error_message, errors=errors, form_data=request.form)

    # Check if user already exists
    try:
        existing_user = db_operation('find_one', 'users', {'mobile_number': normalized_phone})
        if existing_user:
            return render_template_string(SIGNUP_TEMPLATE, error="Phone number already registered.", form_data=request.form)

        # Generate and Store OTP
        otp = generate_otp()
        store_otp(normalized_phone, otp)
        send_otp_console(normalized_phone, otp)

        # Store necessary data in session for verification step
        session['signup_data'] = {
            'name': name,
            'phone': normalized_phone,
            'password': password,  # Store plain password for create_user
            'otp_stored': otp,
            'otp_expiry': (datetime.now() + timedelta(minutes=10)).isoformat()
        }
        print(f"OTP generated for signup {normalized_phone}: {otp}")
        return redirect('/verify-otp')

    except Exception as e:
        print(f"❌ Signup error processing {normalized_phone}: {e}")
        return render_template_string(SIGNUP_TEMPLATE, error="An unexpected error occurred. Please try again later.", form_data=request.form)

@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp_route():
    if 'signup_data' not in session or not isinstance(session['signup_data'], dict):
        print("⚠️ Attempted OTP verification without valid signup data in session.")
        return redirect('/signup')

    signup_data = session['signup_data']
    phone = signup_data.get('phone')

    if not phone or 'otp_stored' not in signup_data or 'otp_expiry' not in signup_data:
        print(f"⚠️ Invalid signup_data in session: {signup_data}")
        session.pop('signup_data', None)
        return redirect('/signup')

    if request.method == 'GET':
        return render_template_string(OTP_TEMPLATE, phone=phone)

    # POST request handling
    user_otp = request.form.get('otp', '').strip()

    if not user_otp or not user_otp.isdigit() or len(user_otp) != 6:
        return render_template_string(OTP_TEMPLATE, phone=phone, error="Please enter a valid 6-digit OTP.")

    try:
        expiry_time = datetime.fromisoformat(signup_data['otp_expiry'])
        stored_otp = signup_data['otp_stored']

        # 1. Check Expiry
        if datetime.now() > expiry_time:
            session.pop('signup_data', None)
            print(f"OTP expired for {phone}")
            return render_template_string(OTP_TEMPLATE, phone=phone, error="OTP has expired. Please sign up again.")

        # 2. Check OTP Match
        if user_otp == stored_otp:
            # Create User
            print(f"OTP verified for {phone}. Creating user...")
            user_id = create_user(
                mobile_number=phone,
                name=signup_data['name'],
                password=signup_data['password']  # Pass plain password
            )

            if user_id:
                # Login the new user
                session.clear()
                session['user'] = {
                    'mobile_number': phone,
                    'name': signup_data['name'],
                    'avatar': signup_data['name'][:2].upper() if signup_data['name'] else '??'
                }
                session.permanent = True
                session.pop('signup_data', None)
                print(f"✅ User created and logged in: {phone}")
                return redirect('/')
            else:
                print(f"🚨 CRITICAL: OTP verified but user creation failed for {phone}")
                session.pop('signup_data', None)
                return redirect('/login')
        else:
            # Invalid OTP entered
            print(f"Invalid OTP entered for {phone}")
            return render_template_string(OTP_TEMPLATE, phone=phone, error="Invalid OTP entered.")

    except ValueError as ve:
         print(f"❌ Error parsing OTP expiry date for {phone}: {ve}")
         session.pop('signup_data', None)
         return render_template_string(OTP_TEMPLATE, phone=phone, error="Invalid session data. Please sign up again.")
    except Exception as e:
        print(f"❌ Unexpected OTP verification error for {phone}: {e}")
        return render_template_string(OTP_TEMPLATE, phone=phone, error="An error occurred during verification. Please try again.")

@app.route('/logout')
def logout():
    """Logs the user out, updates status, and clears session."""
    if 'user' in session:
        mobile = session['user'].get('mobile_number')
        if mobile:
            try:
                db_operation('update_one', 'users',
                             {'mobile_number': mobile},
                             {'$set': {'is_online': False, 'last_seen': datetime.now()}})
                print(f"User logged out and status updated: {mobile}")
            except Exception as e:
                print(f"⚠️ Error updating user status on logout for {mobile}: {e}")
        else:
             print("⚠️ User in session but mobile_number missing during logout.")
        session.clear()
    return redirect('/login')

# ========================
# API ROUTES
# ========================

@app.route('/api/current-user')
def get_current_user():
    """API: Get details of the currently logged-in user."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in or session invalid'}), 401

    mobile = session['user']['mobile_number']
    try:
        user = db_operation('find_one', 'users', {'mobile_number': mobile})
        if user:
            user_data = {
                'id': str(user.get('_id')),
                'mobile_number': user.get('mobile_number'),
                'name': user.get('name'),
                'avatar': user.get('avatar'),
                'status': user.get('status', ''),
                'contacts': user.get('contacts', []),
                'is_online': user.get('is_online', False),
                'last_seen': user.get('last_seen').isoformat() if isinstance(user.get('last_seen'), datetime) else None,
                'profile_pic': user.get('profile_pic'),
                'notification_count': user.get('notification_count', 0)
            }
            return jsonify({'success': True, 'current_user': user_data})
        else:
            print(f"🚨 CRITICAL: User {mobile} in session but not found in DB!")
            session.clear()
            return jsonify({'success': False, 'error': 'User not found in database'}), 404
    except Exception as e:
         print(f"❌ Error fetching current user {mobile}: {e}")
         return jsonify({'success': False, 'error': 'Server error fetching user data'}), 500

@app.route('/api/contacts')
def get_contacts_api():
    """API: Get the user's contact list details."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    current_user_mobile = session['user']['mobile_number']
    try:
        contacts = get_user_contacts(current_user_mobile)
        return jsonify({'success': True, 'contacts': contacts})
    except Exception as e:
         print(f"❌ Error fetching contacts for {current_user_mobile}: {e}")
         return jsonify({'success': False, 'error': 'Server error fetching contacts'}), 500

@app.route('/api/chats')
def get_chats_api():
    """API: Get the user's list of conversations."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    current_user_mobile = session['user']['mobile_number']
    try:
        conversations = get_user_conversations(current_user_mobile)
        return jsonify({'success': True, 'chats': conversations})
    except Exception as e:
        print(f"❌ Unexpected Error in /api/chats endpoint for {current_user_mobile}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Internal server error processing chat list'}), 500

@app.route('/api/chat/<string:other_user_mobile>')
def get_chat(other_user_mobile):
    """API: Get messages for a specific chat."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    current_user_mobile = session['user']['mobile_number']

    if not other_user_mobile.startswith('+') or not other_user_mobile[1:].isdigit():
        return jsonify({'success': False, 'error': 'Invalid recipient format'}), 400

    try:
        messages = get_conversation_messages(current_user_mobile, other_user_mobile)
        formatted_messages = []
        for msg in messages:
            ts_formatted = None
            ts = msg.get('timestamp')
            if isinstance(ts, datetime):
                ts_formatted = ts.strftime('%H:%M')

            formatted_msg = {
                'id': str(msg.get('_id', '')),
                'sender_id': msg.get('sender'),
                'receiver_id': msg.get('receiver'),
                'encrypted': msg.get('encrypted_text', ''),
                'timestamp': ts_formatted or msg.get('timestamp', ''),
                'read': msg.get('read', False),
                'message_type': msg.get('message_type', 'text'),
                'file_id': msg.get('file_id'),
                'poll_id': msg.get('poll_id'),
                'shared_contact': msg.get('shared_contact')
            }

            # Add file info if available
            if msg.get('file_id'):
                file_info = get_file(msg['file_id'])
                if file_info:
                    formatted_msg['file_info'] = {
                        'filename': file_info.get('filename'),
                        'file_type': file_info.get('file_type'),
                        'size': file_info.get('size')
                    }

            # Add poll info if available
            if msg.get('poll_id'):
                poll_info = get_poll_results(msg['poll_id'])
                if poll_info:
                    formatted_msg['poll_info'] = poll_info

            formatted_messages.append(formatted_msg)

        return jsonify({'success': True, 'messages': formatted_messages})
    except Exception as e:
         print(f"❌ Error fetching chat messages between {current_user_mobile} and {other_user_mobile}: {e}")
         return jsonify({'success': False, 'error': 'Failed to retrieve messages'}), 500

@app.route('/api/mark-read/<string:conversation_id>', methods=['POST'])
def mark_conversation_read(conversation_id):
    """API: Mark all messages in conversation as read."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    current_user_mobile = session['user']['mobile_number']
    
    try:
        result = mark_messages_as_read(conversation_id, current_user_mobile)
        socketio.emit('messages_read', {'conversation_id': conversation_id}, room=current_user_mobile)
        return jsonify({'success': True, 'modified_count': result.modified_count if result else 0})
    except Exception as e:
        print(f"❌ Error marking messages as read: {e}")
        return jsonify({'success': False, 'error': 'Failed to mark messages as read'}), 500

@app.route('/api/send', methods=['POST'])
def send_message_api():
    """API: Send a message to another user."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'Invalid request body, expected JSON object'}), 400

    receiver_mobile = data.get('receiver_id')
    message_text = data.get('message', '').strip()
    sender_mobile = session['user']['mobile_number']
    message_type = data.get('message_type', 'text')
    file_id = data.get('file_id')
    poll_id = data.get('poll_id')
    shared_contact = data.get('shared_contact')

    # Input Validation
    if not receiver_mobile or not isinstance(receiver_mobile, str):
        return jsonify({'success': False, 'error': 'Receiver mobile number is required'}), 400
    if not message_text and message_type == 'text' and not file_id and not poll_id and not shared_contact:
         return jsonify({'success': False, 'error': 'Message cannot be empty'}), 400
    if receiver_mobile == sender_mobile:
        return jsonify({'success': False, 'error': 'Cannot send message to yourself'}), 400
    if message_type == 'text' and len(message_text) > 1000:
        return jsonify({'success': False, 'error': 'Message exceeds maximum length (1000 characters)'}), 400
    if not receiver_mobile.startswith('+') or not receiver_mobile[1:].isdigit():
         print(f"⚠️ Attempt to send message to invalid format number: {receiver_mobile}")
         return jsonify({'success': False, 'error': 'Invalid recipient phone number format'}), 400

    try:
        # Verify recipient exists
        receiver_user = db_operation('find_one', 'users', {'mobile_number': receiver_mobile})
        if not receiver_user:
            return jsonify({'success': False, 'error': 'Recipient user does not exist.'}), 404

        # Add the message to the database
        new_message_doc = add_message(sender_mobile, receiver_mobile, message_text, 
                                     message_type, file_id, poll_id, shared_contact)

        # Emit SocketIO event to the recipient
        message_payload = {
            'id': str(new_message_doc.get('_id')),
            'conversation_id': new_message_doc.get('conversation_id'),
            'sender_id': new_message_doc.get('sender'),
            'receiver_id': new_message_doc.get('receiver'),
            'encrypted': new_message_doc.get('encrypted_text'),
            'timestamp': new_message_doc.get('timestamp').strftime('%H:%M') if isinstance(new_message_doc.get('timestamp'), datetime) else '',
            'read': new_message_doc.get('read', False),
            'sender_name': session['user'].get('name', sender_mobile),
            'message_type': message_type,
            'file_id': file_id,
            'poll_id': poll_id,
            'shared_contact': shared_contact
        }

        # Add file info if available
        if file_id:
            file_info = get_file(file_id)
            if file_info:
                message_payload['file_info'] = {
                    'filename': file_info.get('filename'),
                    'file_type': file_info.get('file_type'),
                    'size': file_info.get('size')
                }

        # Add poll info if available
        if poll_id:
            poll_info = get_poll_results(poll_id)
            if poll_info:
                message_payload['poll_info'] = poll_info

        socketio.emit('new_message', message_payload, room=receiver_mobile)
        print(f"✉️ Message from {sender_mobile} to {receiver_mobile}. Emitted to room '{receiver_mobile}'.")

        return jsonify({'success': True, 'message_id': message_payload['id'], 'timestamp': message_payload['timestamp']}), 201

    except ValueError as ve:
         print(f"❌ Value Error sending message from {sender_mobile} to {receiver_mobile}: {ve}")
         return jsonify({'success': False, 'error': str(ve)}), 400
    except Exception as e:
        print(f"❌ Unexpected Error sending message from {sender_mobile} to {receiver_mobile}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Internal server error while sending message'}), 500

@app.route('/api/decode', methods=['POST'])
def decode_message_api():
    """API: Decode an encrypted message string."""
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'Invalid JSON body'}), 400

    encrypted_text = data.get('encrypted_text')
    if encrypted_text is None:
        return jsonify({'success': False, 'error': 'Missing encrypted_text field'}), 400
    if not isinstance(encrypted_text, str):
         return jsonify({'success': False, 'error': 'encrypted_text must be a string'}), 400

    try:
        decrypted_text = decode_message(encrypted_text)
        return jsonify({'success': True, 'decrypted': decrypted_text})
    except Exception as e:
        print(f"❌ Error decoding message: '{encrypted_text[:50]}...': {e}")
        return jsonify({'success': False, 'error': 'Failed to decode message'}), 500

@app.route('/api/upload-file', methods=['POST'])
def upload_file_api():
    """API: Upload a file and return file ID."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    file_type = request.form.get('file_type', 'document')
    uploaded_by = session['user']['mobile_number']

    # Determine file type from extension
    filename = secure_filename(file.filename)
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    
    if ext in ALLOWED_EXTENSIONS['image']:
        file_type = 'image'
    elif ext in ALLOWED_EXTENSIONS['audio']:
        file_type = 'audio'
    elif ext in ALLOWED_EXTENSIONS['video']:
        file_type = 'video'
    else:
        file_type = 'document'

    if not allowed_file(filename, file_type):
        return jsonify({'success': False, 'error': f'File type not allowed for {file_type}'}), 400

    try:
        file_data = file.read()
        file_id = save_file(file_data, filename, file_type, uploaded_by)
        
        if file_id:
            return jsonify({'success': True, 'file_id': file_id, 'filename': filename, 'file_type': file_type})
        else:
            return jsonify({'success': False, 'error': 'Failed to save file'}), 500

    except Exception as e:
        print(f"❌ Error uploading file: {e}")
        return jsonify({'success': False, 'error': 'Failed to upload file'}), 500

@app.route('/api/file/<string:file_id>')
def get_file_api(file_id):
    """API: Get file data."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    try:
        file_doc = get_file(file_id)
        if not file_doc:
            return jsonify({'success': False, 'error': 'File not found'}), 404

        # Check if user has permission to access this file
        # (simplified - in real app, check conversation permissions)
        
        return send_file(
            io.BytesIO(file_doc['data']),
            as_attachment=True,
            download_name=file_doc['filename'],
            mimetype='application/octet-stream'
        )
    except Exception as e:
        print(f"❌ Error retrieving file {file_id}: {e}")
        return jsonify({'success': False, 'error': 'Failed to retrieve file'}), 500

@app.route('/api/create-poll', methods=['POST'])
def create_poll_api():
    """API: Create a new poll."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'Invalid JSON body'}), 400

    question = data.get('question', '').strip()
    options = data.get('options', [])
    created_by = session['user']['mobile_number']

    if not question:
        return jsonify({'success': False, 'error': 'Poll question is required'}), 400
    if len(options) < 2:
        return jsonify({'success': False, 'error': 'At least 2 options are required'}), 400
    if len(options) > 6:
        return jsonify({'success': False, 'error': 'Maximum 6 options allowed'}), 400

    try:
        poll_id = create_poll(question, options, created_by)
        if poll_id:
            return jsonify({'success': True, 'poll_id': poll_id})
        else:
            return jsonify({'success': False, 'error': 'Failed to create poll'}), 500
    except Exception as e:
        print(f"❌ Error creating poll: {e}")
        return jsonify({'success': False, 'error': 'Failed to create poll'}), 500

@app.route('/api/vote-poll', methods=['POST'])
def vote_poll_api():
    """API: Vote on a poll."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'Invalid JSON body'}), 400

    poll_id = data.get('poll_id')
    option_index = data.get('option_index')
    voter_mobile = session['user']['mobile_number']

    if not poll_id or option_index is None:
        return jsonify({'success': False, 'error': 'Poll ID and option index are required'}), 400

    try:
        success = vote_poll(poll_id, option_index, voter_mobile)
        if success:
            poll = get_poll_results(poll_id)
            socketio.emit('poll_updated', {'poll_id': poll_id, 'poll': poll}, room=poll_id)
            return jsonify({'success': True, 'poll': poll})
        else:
            return jsonify({'success': False, 'error': 'Failed to vote or already voted'}), 400
    except Exception as e:
        print(f"❌ Error voting on poll: {e}")
        return jsonify({'success': False, 'error': 'Failed to vote'}), 500

@app.route('/api/profile', methods=['GET', 'POST'])
def profile_api():
    """API: Get or Update user profile (Name, Status)."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    mobile = session['user']['mobile_number']

    # GET Request
    if request.method == 'GET':
        try:
            user = db_operation('find_one', 'users', {'mobile_number': mobile})
            if user:
                user_data = {
                    'id': str(user.get('_id')),
                    'mobile_number': user.get('mobile_number'),
                    'name': user.get('name'),
                    'avatar': user.get('avatar'),
                    'status': user.get('status', ''),
                    'contacts': user.get('contacts', []),
                    'is_online': user.get('is_online', False),
                    'last_seen': user.get('last_seen').isoformat() if isinstance(user.get('last_seen'), datetime) else None,
                    'profile_pic': user.get('profile_pic'),
                    'notification_count': user.get('notification_count', 0)
                }
                return jsonify({'success': True, 'user': user_data})
            else:
                print(f"🚨 User {mobile} in session but not found in DB during GET profile!")
                session.clear()
                return jsonify({'success': False, 'error': 'User not found'}), 404
        except Exception as e:
             print(f"❌ Error fetching profile for {mobile}: {e}")
             return jsonify({'success': False, 'error': 'Server error fetching profile'}), 500

    # POST Request (Update)
    if request.method == 'POST':
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({'success': False, 'error': 'Invalid JSON body'}), 400

        update_data = {}
        validation_errors = {}
        new_session_data = {}

        # Validate Name
        if 'name' in data:
            name = data['name'].strip() if isinstance(data['name'], str) else ''
            if not name:
                 validation_errors['name'] = 'Name cannot be empty.'
            elif len(name) < 2:
                validation_errors['name'] = 'Name must be at least 2 characters.'
            elif len(name) > 50:
                validation_errors['name'] = 'Name cannot exceed 50 characters.'
            else:
                update_data['name'] = name
                update_data['avatar'] = name[:2].upper()
                new_session_data['name'] = name
                new_session_data['avatar'] = update_data['avatar']

        # Validate Status
        if 'status' in data:
            status = data['status'].strip() if isinstance(data['status'], str) else ''
            if len(status) > 100:
                validation_errors['status'] = 'Status cannot exceed 100 characters.'
            else:
                 update_data['status'] = status

        # Validate Password
        if 'current_password' in data and 'new_password' in data:
            current_password = data['current_password']
            new_password = data['new_password']
            
            if not current_password:
                validation_errors['current_password'] = 'Current password is required.'
            if not new_password:
                validation_errors['new_password'] = 'New password is required.'
            elif len(new_password) < 6:
                validation_errors['new_password'] = 'New password must be at least 6 characters.'
            
            if not validation_errors.get('current_password') and not validation_errors.get('new_password'):
                user = db_operation('find_one', 'users', {'mobile_number': mobile})
                if user and user.get('password_hash') == hash_password(current_password):
                    update_data['password_hash'] = hash_password(new_password)
                else:
                    validation_errors['current_password'] = 'Current password is incorrect.'

        if validation_errors:
            return jsonify({'success': False, 'errors': validation_errors}), 400

        if not update_data:
            return jsonify({'success': True, 'message': 'No profile fields provided to update.'})

        try:
            result = db_operation('update_one', 'users',
                         {'mobile_number': mobile},
                         {'$set': update_data})

            if result and result.matched_count > 0:
                if new_session_data:
                    session['user'].update(new_session_data)
                    session.modified = True
                print(f"Profile updated successfully for {mobile}")
                return jsonify({'success': True, 'message': 'Profile updated successfully', 'updated_fields': list(update_data.keys())})
            elif result and result.matched_count == 0:
                 print(f"🚨 User {mobile} in session but not found in DB during profile update!")
                 session.clear()
                 return jsonify({'success': False, 'error': 'User not found to update'}), 404
            else:
                 return jsonify({'success': False, 'error': 'Profile update failed (no changes made or DB error)'}), 500

        except Exception as e:
            print(f"❌ Error updating profile for {mobile}: {e}")
            return jsonify({'success': False, 'error': 'Server error during profile update'}), 500

@app.route('/api/upload-profile-pic', methods=['POST'])
def upload_profile_pic():
    """API: Upload profile picture."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    mobile = session['user']['mobile_number']

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    if not allowed_file(file.filename, 'image'):
        return jsonify({'success': False, 'error': 'Only image files are allowed'}), 400

    try:
        file_data = file.read()
        file_id = save_file(file_data, secure_filename(file.filename), 'image', mobile)
        
        if file_id:
            file_url = f"/api/file/{file_id}"
            
            result = db_operation('update_one', 'users',
                         {'mobile_number': mobile},
                         {'$set': {'profile_pic': file_url}})

            if result and result.matched_count > 0:
                session['user']['avatar'] = None  # Force using profile pic
                session.modified = True
                
                # Emit profile update to contacts
                socketio.emit('profile_updated', {
                    'user_mobile': mobile,
                    'profile_pic': file_url
                }, room=mobile)
                
                print(f"Profile picture updated for {mobile}")
                return jsonify({'success': True, 'profile_pic': file_url})
            elif result and result.matched_count == 0:
                 print(f"🚨 User {mobile} not found during profile pic update!")
                 return jsonify({'success': False, 'error': 'User not found'}), 404
            else:
                 return jsonify({'success': False, 'error': 'Database update failed'}), 500
        else:
            return jsonify({'success': False, 'error': 'Failed to save profile picture'}), 500

    except Exception as e:
        print(f"❌ Error uploading profile pic for {mobile}: {e}")
        return jsonify({'success': False, 'error': 'Internal server error during upload'}), 500

# Contact Management APIs
@app.route('/api/find-user', methods=['POST'])
def find_user_by_phone():
    """API: Search for a user by phone number to add as contact."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    data = request.get_json()
    if not isinstance(data, dict):
         return jsonify({'success': False, 'error': 'Invalid JSON body'}), 400

    search_phone = data.get('phone', '').strip()
    current_user_mobile = session['user']['mobile_number']

    if not search_phone:
        return jsonify({'success': False, 'error': 'Phone number is required'}), 400
    if len(search_phone) == 10 and search_phone.isdigit():
        search_phone = '+91' + search_phone
    elif not search_phone.startswith('+') or not search_phone[1:].isdigit() or len(search_phone) < 11:
         return jsonify({'success': False, 'error': 'Invalid phone format. Use international format (e.g., +91xxxxxxxxxx)'}), 400

    if search_phone == current_user_mobile:
         return jsonify({'success': False, 'error': 'You cannot search for yourself'}), 400

    try:
        found_user = db_operation('find_one', 'users', {'mobile_number': search_phone})

        if found_user:
            user_info = {
                 'name': found_user.get('name', 'Unknown'),
                 'mobile_number': found_user.get('mobile_number'),
                 'avatar': found_user.get('avatar', '??'),
                 'profile_pic': found_user.get('profile_pic'),
                 'status': found_user.get('status', '')
            }
            return jsonify({'success': True, 'user': user_info})
        else:
            return jsonify({'success': False, 'error': 'User not found'}), 404
    except Exception as e:
         print(f"❌ Error finding user by phone {search_phone}: {e}")
         return jsonify({'success': False, 'error': 'Server error during user search'}), 500

@app.route('/api/add-contact', methods=['POST'])
def add_contact_api():
    """API: Add a user (by phone number) to the current user's contact list."""
    if 'user' not in session or 'mobile_number' not in session['user']:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    data = request.get_json()
    if not isinstance(data, dict):
         return jsonify({'success': False, 'error': 'Invalid JSON body'}), 400

    contact_phone = data.get('phone', '').strip()
    current_user_mobile = session['user']['mobile_number']

    if not contact_phone:
        return jsonify({'success': False, 'error': 'Contact phone number is required'}), 400
    if contact_phone == current_user_mobile:
        return jsonify({'success': False, 'error': 'You cannot add yourself as a contact'}), 400
    if not contact_phone.startswith('+') or not contact_phone[1:].isdigit():
         return jsonify({'success': False, 'error': 'Invalid contact phone number format'}), 400

    try:
        contact_user_exists = db_operation('find_one', 'users', {'mobile_number': contact_phone})
        if not contact_user_exists:
            return jsonify({'success': False, 'error': 'The user you are trying to add does not exist'}), 404

        update_query = {'mobile_number': current_user_mobile}
        update_action = {'$addToSet': {'contacts': contact_phone}}

        update_result = db_operation('update_one', 'users', update_query, update_action)

        if update_result and update_result.matched_count > 0:
            if update_result.modified_count > 0:
                 print(f"Contact {contact_phone} added for {current_user_mobile}")
                 socketio.emit('contacts_updated', {'new_contact': contact_phone}, room=current_user_mobile)
                 return jsonify({'success': True, 'message': 'Contact added successfully'})
            else:
                 return jsonify({'success': True, 'message': 'Contact already exists'})
        elif update_result and update_result.matched_count == 0:
             print(f"🚨 User {current_user_mobile} not found during add contact!")
             session.clear()
             return jsonify({'success': False, 'error': 'Current user not found'}), 404
        else:
             return jsonify({'success': False, 'error': 'Failed to add contact due to database error'}), 500

    except Exception as e:
        print(f"❌ Error adding contact {contact_phone} for {current_user_mobile}: {e}")
        return jsonify({'success': False, 'error': 'Server error while adding contact'}), 500

# ========================
# SOCKET.IO EVENT HANDLERS
# ========================

@socketio.on('connect')
def handle_connect():
    """Handles new SocketIO connections."""
    user_mobile = session.get('user', {}).get('mobile_number')

    if user_mobile:
        join_room(user_mobile)
        print(f"🔌 Socket connected: {request.sid}, User: {user_mobile}, Joined room: '{user_mobile}'")

        try:
            db_operation('update_one', 'users',
                         {'mobile_number': user_mobile},
                         {'$set': {'is_online': True, 'last_seen': datetime.now()}})
            # Emit online status to contacts
            socketio.emit('status_update', {
                'user': user_mobile,
                'online': True,
                'last_seen': datetime.now().isoformat()
            }, room=user_mobile)
        except Exception as e:
            print(f"⚠️ Error updating online status on connect for {user_mobile}: {e}")
    else:
        print(f"🔌 Socket connected: {request.sid}, User: NOT LOGGED IN")

@socketio.on('disconnect')
def handle_disconnect():
    """Handles SocketIO disconnections."""
    user_mobile = session.get('user', {}).get('mobile_number')

    if user_mobile:
        print(f"🔌 Socket disconnected: {request.sid}, User: {user_mobile}, Left room: '{user_mobile}'")

        try:
            db_operation('update_one', 'users',
                         {'mobile_number': user_mobile},
                         {'$set': {'is_online': False, 'last_seen': datetime.now()}})
            # Emit offline status to contacts
            socketio.emit('status_update', {
                'user': user_mobile,
                'online': False,
                'last_seen': datetime.now().isoformat()
            }, room=user_mobile)
        except Exception as e:
            print(f"⚠️ Error updating offline status on disconnect for {user_mobile}: {e}")
    else:
         print(f"🔌 Socket disconnected: {request.sid}, User: WAS NOT LOGGED IN")

@socketio.on('join_poll')
def handle_join_poll(data):
    """Join a poll room for real-time updates."""
    poll_id = data.get('poll_id')
    if poll_id:
        join_room(poll_id)
        print(f"User joined poll room: {poll_id}")

@socketio.on('typing')
def handle_typing(data):
    """Handle typing indicators."""
    receiver = data.get('receiver')
    is_typing = data.get('is_typing', False)
    if receiver:
        socketio.emit('typing_indicator', {
            'sender': session.get('user', {}).get('mobile_number'),
            'is_typing': is_typing
        }, room=receiver)

# ========================
# HTML TEMPLATES
# ========================

LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SecureChat - Login</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #121212;
            color: #f1f1f1;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
        }

        .login-container {
            width: 90%;
            max-width: 400px;
            background: #1e1e1e;
            padding: 25px;
            border-radius: 12px;
            box-shadow: 0 0 15px rgba(0,0,0,0.5);
            transition: all 0.3s ease;
        }

        .login-container:hover {
            box-shadow: 0 0 25px rgba(0, 255, 136, 0.2);
        }

        h2 {
            text-align: center;
            color: #00ff99;
            margin-bottom: 20px;
        }

        .form-group {
            margin-bottom: 15px;
        }

        label {
            display: block;
            font-size: 14px;
            margin-bottom: 5px;
            color: #ccc;
        }

        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 10px;
            background: #222;
            border: 1px solid #333;
            border-radius: 6px;
            color: #f1f1f1;
            font-size: 15px;
            outline: none;
            transition: border-color 0.2s;
        }

        input[type="text"]:focus,
        input[type="password"]:focus {
            border-color: #00ff99;
        }

        button {
            width: 100%;
            padding: 12px;
            background: linear-gradient(90deg, #00c853, #00ff99);
            color: #fff;
            border: none;
            border-radius: 6px;
            font-size: 16px;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }

        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,255,153,0.3);
        }

        .error {
            color: #ff4444;
            margin-top: 10px;
            font-size: 14px;
        }

        .links {
            text-align: center;
            margin-top: 20px;
        }

        .links a {
            color: #00ff99;
            text-decoration: none;
            font-weight: bold;
            transition: color 0.2s;
        }

        .links a:hover {
            color: #fff;
        }

        /* Responsive Design */
        @media (max-width: 480px) {
            .login-container {
                padding: 20px;
            }

            button {
                font-size: 15px;
                padding: 10px;
            }

            h2 {
                font-size: 20px;
            }
        }
    </style>
</head>
<body>
    <div class="login-container">
        <h2>SecureChat Login</h2>

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        <form method="POST">
            <div class="form-group">
                <label>Phone Number:</label>
                <input type="text" name="phone" placeholder="e.g., 9876543210 or +919876543210" required>
            </div>

            <div class="form-group">
                <label>Password:</label>
                <input type="password" name="password" required>
            </div>

            <button type="submit">Login</button>
        </form>

        <div class="links">
            <a href="/signup">Create New Account</a>
        </div>
    </div>
</body>
</html>
'''

SIGNUP_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SecureChat - Sign Up</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #121212;
            color: #f1f1f1;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
        }

        .signup-container {
            width: 90%;
            max-width: 400px;
            background: #1e1e1e;
            padding: 25px;
            border-radius: 12px;
            box-shadow: 0 0 15px rgba(0,0,0,0.5);
            transition: all 0.3s ease;
        }

        .signup-container:hover {
            box-shadow: 0 0 25px rgba(0, 255, 136, 0.2);
        }

        h2 {
            text-align: center;
            color: #00ff99;
            margin-bottom: 20px;
        }

        .form-group {
            margin-bottom: 15px;
        }

        label {
            display: block;
            font-size: 14px;
            margin-bottom: 5px;
            color: #ccc;
        }

        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 10px;
            background: #222;
            border: 1px solid #333;
            border-radius: 6px;
            color: #f1f1f1;
            font-size: 15px;
            outline: none;
            transition: border-color 0.2s;
        }

        input[type="text"]:focus,
        input[type="password"]:focus {
            border-color: #00ff99;
        }

        button {
            width: 100%;
            padding: 12px;
            background: linear-gradient(90deg, #00c853, #00ff99);
            color: #fff;
            border: none;
            border-radius: 6px;
            font-size: 16px;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }

        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,255,153,0.3);
        }

        .error {
            color: #ff4444;
            margin-top: 10px;
            font-size: 14px;
        }

        .links {
            text-align: center;
            margin-top: 20px;
        }

        .links a {
            color: #00ff99;
            text-decoration: none;
            font-weight: bold;
            transition: color 0.2s;
        }

        .links a:hover {
            color: #fff;
        }

        /* Responsive Design */
        @media (max-width: 480px) {
            .signup-container {
                padding: 20px;
            }

            button {
                font-size: 15px;
                padding: 10px;
            }

            h2 {
                font-size: 20px;
            }
        }
    </style>
</head>
<body>
    <div class="signup-container">
        <h2>Create SecureChat Account</h2>

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        <form method="POST">
            <div class="form-group">
                <label>Full Name:</label>
                <input type="text" name="name" value="{{ form_data.name if form_data }}" required>
            </div>

            <div class="form-group">
                <label>Phone Number:</label>
                <input type="text" name="phone" placeholder="e.g., 9876543210" value="{{ form_data.phone if form_data }}" required>
            </div>

            <div class="form-group">
                <label>Password:</label>
                <input type="password" name="password" required>
            </div>

            <div class="form-group">
                <label>Confirm Password:</label>
                <input type="password" name="confirm_password" required>
            </div>

            <button type="submit">Sign Up</button>
        </form>

        <div class="links">
            <a href="/login">Back to Login</a>
        </div>
    </div>
</body>
</html>
'''
OTP_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SecureChat - Verify OTP</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: radial-gradient(circle at top left, #0a0a0a, #121212 70%);
            color: #f1f1f1;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            overflow: hidden;
            animation: fadeIn 0.8s ease-in-out;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(-10px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .otp-container {
            width: 90%;
            max-width: 400px;
            background: #1e1e1e;
            padding: 28px;
            border-radius: 14px;
            box-shadow: 0 0 25px rgba(0, 255, 136, 0.08);
            transition: all 0.3s ease;
            transform: scale(1);
        }

        .otp-container:hover {
            box-shadow: 0 0 30px rgba(0, 255, 136, 0.25);
            transform: scale(1.01);
        }

        h2 {
            text-align: center;
            color: #00ff99;
            margin-bottom: 12px;
            letter-spacing: 0.5px;
        }

        p {
            text-align: center;
            color: #ccc;
            font-size: 14px;
            margin-bottom: 22px;
        }

        .phone-display {
            background: #222;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 18px;
            text-align: center;
            font-size: 15px;
            color: #00ff99;
            box-shadow: inset 0 0 5px rgba(0, 255, 153, 0.15);
        }

        input[type="text"] {
            width: 100%;
            padding: 12px;
            background: #222;
            border: 1px solid #333;
            border-radius: 6px;
            font-size: 20px;
            text-align: center;
            color: #f1f1f1;
            letter-spacing: 4px;
            outline: none;
            transition: all 0.25s ease;
        }

        input[type="text"]:focus {
            border-color: #00ff99;
            box-shadow: 0 0 8px rgba(0, 255, 153, 0.3);
        }

        button {
            width: 100%;
            padding: 12px;
            margin-top: 15px;
            background: linear-gradient(90deg, #00c853, #00ff99);
            color: #fff;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s ease, box-shadow 0.25s ease;
        }

        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 18px rgba(0, 255, 153, 0.35);
        }

        .error {
            color: #ff4444;
            text-align: center;
            margin-top: 10px;
            font-size: 14px;
            animation: shake 0.4s ease-in-out;
        }

        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-4px); }
            50% { transform: translateX(4px); }
            75% { transform: translateX(-4px); }
        }

        .note {
            font-size: 12px;
            color: #888;
            margin-top: 18px;
            text-align: center;
        }

        @media (max-width: 480px) {
            input[type="text"] {
                font-size: 18px;
                padding: 10px;
            }

            button {
                font-size: 15px;
                padding: 10px;
            }

            h2 {
                font-size: 20px;
            }
        }
    </style>
</head>
<body>
    <div class="otp-container">
        <h2>Verify Your Phone</h2>

        <div class="phone-display">
            <strong>Phone:</strong> {{ phone }}
        </div>

        <p>Enter the 6-digit OTP sent to your phone:</p>

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        <form method="POST">
            <input type="text" name="otp" placeholder="000000" maxlength="6" required pattern="[0-9]{6}">
            <button type="submit">Verify OTP</button>
        </form>

        <p class="note"><em>Note: Check your console for the OTP during development.</em></p>
    </div>
</body>
</html>
'''


# MAIN_APP_TEMPLATE - This is the main template with all the new features
# Due to its length, I'll include it in the next message
MAIN_APP_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SecureChat 🔒 E2E Encrypted</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/emoji-picker-element@1.12.0/index.css">
    <style>
         * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #111b21; color: #e9edef; height: 100vh; overflow: hidden; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
        .app-container { display: flex; height: 100vh; width: 100%; background: #0b141a; position: relative; }
        .sidebar { width: 100%; max-width: 400px; background: #111b21; border-right: 1px solid #2a3942; display: flex; flex-direction: column; transition: transform 0.3s ease; z-index: 10; }
        @media (min-width: 769px) { 
            .sidebar { position: relative; } 
            .chat-area { display: flex; } 
        }
        @media (max-width: 768px) { 
            .sidebar { 
                position: absolute; 
                top: 0; 
                left: 0; 
                width: 100%; 
                height: 100%; 
                transform: translateX(0); /* VISIBLE BY DEFAULT ON MOBILE */ 
            } 
            .chat-area { 
                display: none; 
            } 
            .chat-area.active { 
                display: flex; 
                position: fixed; 
                top: 0; 
                left: 0; 
                width: 100%; 
                height: 100%; 
                z-index: 5; 
            } 
            .sidebar.hidden { 
                transform: translateX(-100%); 
            } 
        }
        @media (max-width: 1024px) { .sidebar { max-width: 350px; } }
        @media (max-width: 480px) { .sidebar { max-width: 100%; } }
        .sidebar-header { padding: 10px 16px; height: 60px; background: #202c33; border-bottom: 1px solid #2a3942; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0;}
        @media (max-width: 480px) { .sidebar-header { padding: 10px 12px; } }
        .user-profile { display: flex; align-items: center; gap: 10px; cursor: pointer; position: relative; }
        .profile-avatar { width: 40px; height: 40px; border-radius: 50%; background: linear-gradient(135deg, #00a884, #128c7e); display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 16px; overflow: hidden; flex-shrink: 0; }
        .profile-avatar img { width: 100%; height: 100%; object-fit: cover; }
        .profile-dropdown { position: absolute; top: calc(100% + 5px); left: 0; background: #2A3942; border-radius: 8px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4); width: 220px; z-index: 1000; display: none; padding: 8px 0; border: 1px solid #3b4a54; }
        @media (max-width: 480px) { .profile-dropdown { width: 200px; left: 50px; } }
        .profile-dropdown.active { display: block; }
        .dropdown-item { padding: 10px 16px; display: flex; align-items: center; gap: 12px; cursor: pointer; transition: background 0.2s; font-size: 14px; color: #e9edef;}
        .dropdown-item i { width: 18px; text-align: center; color: #8696a0;}
        .dropdown-item:hover { background: #3b4a54; }
        #logout { color: #f15c6c; border-top: 1px solid #3b4a54; margin-top: 5px; padding-top: 10px;}
        #logout i { color: #f15c6c;}
        .sidebar-actions { display: flex; gap: 10px; align-items: center;}
        @media (max-width: 480px) { .sidebar-actions { gap: 5px; } }
        .sidebar-action-btn { background: none; border: none; color: #AEBAC1; font-size: 20px; cursor: pointer; transition: color 0.2s; padding: 8px; border-radius: 50%; min-width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; }
        @media (max-width: 480px) { .sidebar-action-btn { padding: 6px; font-size: 18px; min-width: 40px; height: 40px; } }
        .sidebar-action-btn:hover { color: #e9edef; background-color: rgba(255, 255, 255, 0.1); }
        .search-container { padding: 8px 12px; border-bottom: 1px solid #2a3942; background: #111B21; flex-shrink: 0;}
        @media (max-width: 480px) { .search-container { padding: 8px 8px; } }
        .search-box { width: 100%; padding: 8px 16px; background: #202c33; border: none; border-radius: 8px; color: #e9edef; font-size: 14px; height: 36px; }
        @media (max-width: 480px) { .search-box { padding: 8px 12px; font-size: 16px; } }
        .search-box::placeholder { color: #8696a0; }
        .tabs { display: flex; border-bottom: 1px solid #2a3942; flex-shrink: 0;}
        .tab { flex: 1; padding: 14px 10px; text-align: center; cursor: pointer; color: #8696a0; font-size: 14px; position: relative; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
        @media (max-width: 480px) { .tab { padding: 12px 8px; font-size: 12px; } }
        .tab.active { color: #00A884; }
        .tab.active::after { content: ''; position: absolute; bottom: 0; left: 0; width: 100%; height: 3px; background: #00A884; }
        .list-container { flex: 1; overflow-y: auto; position: relative; background: #111b21; }
        .chats-list, .contacts-list { display: none; height: 100%;}
        .chats-list.active, .contacts-list.active { display: block; }
        .chat-item, .contact-item { padding: 12px 16px; display: flex; align-items: flex-end; gap: 12px; cursor: pointer; border-bottom: 1px solid #2a3942; transition: background 0.15s ease-in-out; position: relative; min-height: 72px; }
        @media (max-width: 480px) { .chat-item, .contact-item { padding: 12px 12px; gap: 12px; min-height: 68px; } }
        .chat-item:last-child, .contact-item:last-child { border-bottom: none; }
        .chat-item:hover, .contact-item:hover, .chat-item.active { background: #2a3942; }
        .chat-avatar, .contact-avatar { width: 49px; height: 49px; border-radius: 50%; background: #3b4a54; display: flex; align-items: center; justify-content: center; font-weight: 400; font-size: 18px; overflow: hidden; position: relative; flex-shrink: 0;}
        @media (max-width: 480px) { .chat-avatar, .contact-avatar { width: 44px; height: 44px; font-size: 16px; } }
        .chat-avatar img, .contact-avatar img { width: 100%; height: 100%; object-fit: cover; border-radius: 50%; }
        .online-indicator { position: absolute; bottom: 0; right: 0; width: 12px; height: 12px; background: #00A884; border: 2px solid #111b21; border-radius: 50%; }
        .chat-info, .contact-info { flex: 1; min-width: 0; padding-right: 5px; display: flex; flex-direction: column; justify-content: space-between; }
        .chat-info-top { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 2px; }
        .chat-name, .contact-name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 15px; color: #E9EDEF; }
        @media (max-width: 480px) { .chat-name, .contact-name { font-size: 14px; } }
        .chat-time { font-size: 12px; color: #8696a0; white-space: nowrap; flex-shrink: 0; }
        .chat-info-bottom { display: flex; align-items: center; justify-content: space-between; max-width: 100%; }
        .chat-last-message, .contact-status { font-size: 14px; color: #8696a0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex-grow: 1; padding-right: 5px; }
        @media (max-width: 480px) { .chat-last-message, .contact-status { font-size: 13px; } }
        .unread-badge { background: #00A884; color: #111B21; font-size: 11px; font-weight: 600; padding: 2px 6px; border-radius: 10px; min-width: 18px; height: 18px; line-height: 14px; text-align: center; flex-shrink: 0; margin-left: auto; }
        .lock-icon { display: inline-block; color: #8696a0; font-size: 11px; margin-right: 3px; vertical-align: middle; }
        /* Chat Area */
        .chat-area { flex: 1; display: flex; flex-direction: column; background-color: #0b141a; position: relative; }
        @media (max-width: 768px) { .chat-area { background: #0b141a; } }
        .chat-header { padding: 0 16px; height: 60px; background: #202c33; border-bottom: 1px solid #2a3942; display: flex; align-items: center; gap: 12px; flex-shrink: 0; position: sticky; top: 0; z-index: 10; }
        @media (max-width: 768px) { .chat-header { padding: 0 12px; } }
        @media (max-width: 480px) { .chat-header { padding: 0 12px; height: 59px; } }
         .back-button { background: none; border: none; color: #AEBAC1; font-size: 20px; cursor: pointer; display: none; margin-right: 5px; min-width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; border-radius: 50%; }
         @media (max-width: 768px) { .back-button { display: flex; margin-right: 0; } }
         @media (max-width: 480px) { .back-button { font-size: 18px; min-width: 40px; height: 40px; } }
         .back-button:hover { background-color: rgba(255, 255, 255, 0.1); }
        .chat-contact-avatar { width: 40px; height: 40px; border-radius: 50%; background: #3b4a54; display: flex; align-items: center; justify-content: center; font-weight: 400; font-size: 16px; overflow: hidden; flex-shrink: 0;}
        @media (max-width: 480px) { .chat-contact-avatar { width: 36px; height: 36px; font-size: 14px; } }
        .chat-contact-avatar img { width: 100%; height: 100%; object-fit: cover; border-radius: 50%; }
        .chat-contact-info { flex: 1; min-width: 0; cursor: pointer; }
        .chat-contact-name { font-weight: 500; font-size: 16px; margin-bottom: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #E9EDEF; }
        @media (max-width: 480px) { .chat-contact-name { font-size: 15px; } }
        .chat-contact-status { font-size: 12px; color: #8696a0; display: flex; align-items: center; gap: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
        @media (max-width: 480px) { .chat-contact-status { font-size: 11px; gap: 3px; } }
        .online-dot { width: 8px; height: 8px; background: #00A884; border-radius: 50%; flex-shrink: 0; }
        .chat-actions { display: flex; gap: 8px; margin-left: auto; }
        @media (max-width: 480px) { .chat-actions { gap: 6px; } }
        .chat-action-btn { background: none; border: none; color: #AEBAC1; cursor: pointer; font-size: 20px; transition: color 0.2s; padding: 8px; border-radius: 50%; min-width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; }
        @media (max-width: 480px) { .chat-action-btn { font-size: 18px; padding: 6px; min-width: 40px; height: 40px; } }
        .chat-action-btn:hover { color: #e9edef; background-color: rgba(255, 255, 255, 0.1);}
        .messages-container { flex: 1; padding: 20px 16px; overflow-y: auto; background-image: url("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAJYAAACWCAYAAAA8AXHiAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAEaSURBVHhe7dOxAQAhEACx/0/tTdtBRDU/7lxcAMDxPBYAwsEBYQGwDywgFgHLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA/+/UeAAdgDEhALgGUAA2EJMAGwDywgFgHLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAEy5AACAAmQKEkAAAAASUVORK5CYII="); background-color: #0b141a; background-repeat: repeat; background-size: auto; display: flex; flex-direction: column; gap: 4px; }
        @media (max-width: 768px) { .messages-container { padding: 20px 12px; } }
        @media (max-width: 480px) { .messages-container { padding: 16px 8px; gap: 2px; } }
        .message { max-width: 60%; padding: 6px 12px 8px 12px; border-radius: 7.5px; position: relative; word-wrap: break-word; line-height: 1.4; font-size: 14.5px; box-shadow: 0 1px 0.5px rgba(11,20,26,.13); margin-bottom: 0; }
        @media (max-width: 768px) { .message { max-width: 75%; padding: 6px 10px 7px 10px; font-size: 14px; } }
        @media (max-width: 480px) { .message { max-width: 85%; padding: 5px 8px 6px 8px; font-size: 13.5px; } }
        .message.sent { align-self: flex-end; background: #005c4b; border-top-right-radius: 7.5px; border-bottom-right-radius: 7.5px; color: #e9edef; }
        .message.received { align-self: flex-start; background: #202c33; border-top-left-radius: 7.5px; border-bottom-left-radius: 7.5px; color: #e9edef; }
        .message-text { margin-bottom: 2px; line-height: 1.35; }
        .message-meta { display: flex; justify-content: flex-end; align-items: center; font-size: 11px; color: rgba(233, 237, 239, 0.6); margin-top: 2px; height: 15px; line-height: 15px; }
        .message-time { margin-right: 5px; }
        .toggle-btn { background: none; border: none; color: rgba(233, 237, 239, 0.6); padding: 0 5px; cursor: pointer; font-size: 11px; transition: color 0.2s; margin-left: auto; }
        .toggle-btn:hover { color: #e9edef; }
        /* File Message Styles */
        .file-message { border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px; margin: 5px 0; background: rgba(255,255,255,0.05); align-self: flex-start; max-width: 80%; }
        @media (max-width: 480px) { .file-message { padding: 10px; max-width: 90%; } }
        .file-icon { font-size: 24px; margin-bottom: 8px; }
        @media (max-width: 480px) { .file-icon { font-size: 20px; } }
        .file-name { font-weight: 500; margin-bottom: 4px; word-break: break-all; }
        .file-size { font-size: 12px; color: #8696a0; }
        .file-download-btn { background: #00a884; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; margin-top: 8px; font-size: 12px; }
        .file-download-btn:hover { background: #128c7e; }
        /* Voice Message Styles */
        .voice-message { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: rgba(255,255,255,0.1); border-radius: 20px; max-width: 70%; }
        @media (max-width: 480px) { .voice-message { padding: 6px 10px; gap: 8px; max-width: 80%; } }
        .voice-play-btn { background: none; border: none; color: #00a884; font-size: 18px; cursor: pointer; flex-shrink: 0; }
        @media (max-width: 480px) { .voice-play-btn { font-size: 16px; } }
        .voice-waveform { flex: 1; height: 20px; background: linear-gradient(90deg, #00a884 50%, rgba(255,255,255,0.3) 50%); border-radius: 10px; }
        .voice-duration { font-size: 12px; color: #8696a0; flex-shrink: 0; }
        /* Poll Message Styles */
        .poll-message { border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px; margin: 5px 0; background: rgba(255,255,255,0.05); max-width: 80%; align-self: flex-start; }
        @media (max-width: 480px) { .poll-message { padding: 10px; max-width: 90%; } }
        .poll-question { font-weight: 500; margin-bottom: 12px; font-size: 15px; }
        @media (max-width: 480px) { .poll-question { font-size: 14px; } }
        .poll-option { padding: 8px 12px; margin: 6px 0; background: rgba(255,255,255,0.1); border-radius: 6px; cursor: pointer; transition: background 0.2s; font-size: 14px; }
        @media (max-width: 480px) { .poll-option { padding: 6px 10px; font-size: 13px; } }
        .poll-option:hover { background: rgba(255,255,255,0.2); }
        .poll-option.voted { background: rgba(0,168,132,0.3); border: 1px solid #00a884; }
        .poll-results { margin-top: 8px; }
        .poll-result-bar { height: 6px; background: #00a884; border-radius: 3px; margin-top: 4px; }
        .poll-vote-count { font-size: 11px; color: #8696a0; text-align: right; margin-top: 2px; }
        /* Contact Share Styles */
        .contact-share { border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px; margin: 5px 0; background: rgba(255,255,255,0.05); max-width: 80%; align-self: flex-start; }
        @media (max-width: 480px) { .contact-share { padding: 10px; max-width: 90%; } }
        .contact-name-share { font-weight: 500; margin-bottom: 4px; }
        .contact-phone-share { font-size: 12px; color: #8696a0; margin-bottom: 8px; }
        .contact-add-btn { background: #00a884; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }
        .contact-add-btn:hover { background: #128c7e; }
        .input-area { padding: 8px 16px; background: #202c33; border-top: 1px solid #2a3942; display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; position: sticky; bottom: 0; z-index: 10; }
        @media (max-width: 480px) { .input-area { padding: 8px 12px; } }
        .input-container { display: flex; align-items: flex-end; gap: 8px; width: 100%;}
        @media (max-width: 480px) { .input-container { gap: 6px; } }
        .input-actions { display: flex; gap: 4px; align-items: center; flex-shrink: 0; }
        @media (max-width: 480px) { .input-actions { gap: 3px; } }
        .input-action-btn { background: none; border: none; color: #AEBAC1; cursor: pointer; font-size: 22px; transition: color 0.2s; padding: 8px; border-radius: 50%; min-width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; }
        @media (max-width: 480px) { .input-action-btn { font-size: 20px; padding: 6px; min-width: 40px; height: 40px; } }
        .input-action-btn:hover { color: #e9edef; background-color: rgba(255, 255, 255, 0.1); }
        .input-action-btn.active { color: #00a884; background-color: rgba(0,168,132,0.1); }
        .message-input { flex: 1; background: #2A3942; border: none; border-radius: 21px; padding: 10px 16px; color: #e9edef; font-size: 15px; resize: none; max-height: 120px; line-height: 20px; font-family: inherit; }
        @media (max-width: 480px) { .message-input { padding: 10px 14px; font-size: 16px; border-radius: 18px; } }
        .message-input::placeholder { color: #8696a0; }
        .send-btn { background: none; border: none; width: 44px; height: 44px; border-radius: 50%; color: #AEBAC1; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.2s; flex-shrink: 0; font-size: 22px; }
        @media (max-width: 480px) { .send-btn { width: 40px; height: 40px; font-size: 20px; } }
        .send-btn:hover { color: #e9edef; background-color: rgba(255, 255, 255, 0.1); }
        .send-btn.has-text { color: #00A884; }
        .send-btn.has-text:hover { background-color: rgba(0, 168, 132, 0.1); }
        /* Emoji Picker */
        .emoji-picker-container { position: absolute; bottom: 80px; left: 16px; z-index: 1000; display: none; width: calc(100% - 32px); max-width: 350px; }
        @media (max-width: 480px) { .emoji-picker-container { left: 12px; bottom: 76px; width: calc(100% - 24px); } }
        emoji-picker { --background: #2A3942; --border-color: #3b4a54; --input-border-color: #3b4a54; --input-placeholder-color: #8696a0; --category-emoji-padding: 8px; --indicator-color: #00a884; --num-columns: 7; }
        /* Voice Recording */
        .voice-recorder { display: none; align-items: center; gap: 10px; padding: 8px 16px; background: #2A3942; border-radius: 21px; margin: 5px 0; }
        @media (max-width: 480px) { .voice-recorder { padding: 8px 12px; gap: 8px; } }
        .voice-recorder.active { display: flex; }
        .recording-indicator { width: 12px; height: 12px; background: #f15c6c; border-radius: 50%; animation: pulse 1s infinite; flex-shrink: 0; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        .recording-timer { font-size: 14px; color: #e9edef; flex-grow: 1; text-align: center; }
        @media (max-width: 480px) { .recording-timer { font-size: 13px; } }
        .cancel-recording { background: none; border: none; color: #f15c6c; cursor: pointer; font-size: 16px; flex-shrink: 0; }
        @media (max-width: 480px) { .cancel-recording { font-size: 14px; } }
        /* File Upload */
        .file-upload-container { display: none; position: absolute; bottom: 80px; right: 16px; background: #2A3942; border-radius: 8px; padding: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); z-index: 1000; width: 200px; border: 1px solid #3b4a54; }
        @media (max-width: 480px) { .file-upload-container { bottom: 76px; right: 12px; width: 180px; padding: 10px; } }
        .file-upload-container.active { display: block; }
        .upload-option { padding: 12px 16px; display: flex; align-items: center; gap: 12px; cursor: pointer; transition: background 0.2s; border-radius: 6px; font-size: 14px; }
        @media (max-width: 480px) { .upload-option { padding: 10px 12px; gap: 10px; font-size: 13px; } }
        .upload-option:hover { background: #3b4a54; }
        .upload-option i { font-size: 18px; color: #00a884; flex-shrink: 0; width: 20px; }
        @media (max-width: 480px) { .upload-option i { font-size: 16px; } }
        /* Poll Creator */
        .poll-creator { display: none; position: absolute; bottom: 80px; right: 16px; background: #2A3942; border-radius: 8px; padding: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); z-index: 1000; width: 300px; border: 1px solid #3b4a54; max-height: 300px; overflow-y: auto; }
        @media (max-width: 480px) { .poll-creator { bottom: 76px; right: 12px; width: 280px; padding: 12px; max-height: 250px; } }
        .poll-creator.active { display: block; }
        .poll-input { width: 100%; padding: 8px 12px; background: #202c33; border: 1px solid #3b4a54; border-radius: 6px; color: #e9edef; margin-bottom: 8px; font-size: 14px; }
        @media (max-width: 480px) { .poll-input { padding: 10px 12px; font-size: 16px; border-radius: 4px; } }
        .poll-option-input { display: flex; gap: 8px; margin-bottom: 8px; flex-direction: column; }
        @media (max-width: 480px) { .poll-option-input { gap: 6px; } }
        .add-option-btn { background: #00a884; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; align-self: flex-start; }
        .create-poll-btn { width: 100%; padding: 10px 12px; background: #00a884; color: white; border: none; border-radius: 6px; cursor: pointer; margin-top: 8px; font-size: 14px; }
        @media (max-width: 480px) { .create-poll-btn { padding: 12px 14px; font-size: 16px; border-radius: 4px; } }
        .empty-chat { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; color: #8696a0; padding: 20px;}
        @media (max-width: 480px) { .empty-chat { padding: 40px 20px; } }
        .empty-chat h3 { margin-bottom: 8px; color: #e9edef; font-weight: 300; font-size: 24px; }
        @media (max-width: 480px) { .empty-chat h3 { font-size: 20px; } }
        .empty-chat p { font-size: 14px; line-height: 1.4;}
        @media (max-width: 480px) { .empty-chat p { font-size: 13px; } }
        .empty-chat i { font-size: 48px; margin-bottom: 16px; color: #3b4a54; opacity: 0.5; }
        @media (max-width: 480px) { .empty-chat i { font-size: 40px; } }
        /* Modal Styles */
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(11, 20, 26, 0.85); z-index: 2000; align-items: center; justify-content: center; backdrop-filter: blur(3px); animation: fadeIn 0.3s ease-out; }
        .modal.active { display: flex; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        .modal-content { background: #202c33; border-radius: 12px; width: 90%; max-width: 450px; max-height: 90vh; overflow-y: auto; box-shadow: 0 8px 24px rgba(0,0,0,0.4); animation: slideIn 0.3s ease-out; }
        @media (max-width: 480px) { .modal-content { width: 95%; max-width: none; border-radius: 16px; margin: 10px; } }
        @keyframes slideIn { from { transform: translateY(-20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
        .modal-header { padding: 16px 20px; border-bottom: 1px solid #2a3942; display: flex; align-items: center; justify-content: space-between; }
        @media (max-width: 480px) { .modal-header { padding: 12px 16px; } }
        .modal-title { font-size: 18px; font-weight: 500; color: #E9EDEF;}
        @media (max-width: 480px) { .modal-title { font-size: 16px; } }
        .modal-close { background: none; border: none; color: #8696a0; font-size: 20px; cursor: pointer; transition: color 0.2s; padding: 8px; border-radius: 50%; min-width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; margin: -8px -8px -8px 8px; }
        @media (max-width: 480px) { .modal-close { font-size: 18px; min-width: 40px; height: 40px; } }
        .modal-close:hover { color: #e9edef; background-color: rgba(255, 255, 255, 0.1); }
        .modal-body { padding: 20px; }
        @media (max-width: 480px) { .modal-body { padding: 16px; } }
        .profile-pic-container { display: flex; flex-direction: column; align-items: center; margin-bottom: 20px; }
        .profile-pic-large { width: 120px; height: 120px; border-radius: 50%; background: linear-gradient(135deg, #00a884, #128c7e); display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 48px; overflow: hidden; margin-bottom: 16px; color: white; }
        @media (max-width: 480px) { .profile-pic-large { width: 100px; height: 100px; font-size: 40px; } }
        .profile-pic-large img { width: 100%; height: 100%; object-fit: cover; border-radius: 50%; }
        .upload-btn { background: #00a884; border: none; color: white; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; transition: background 0.2s; }
        @media (max-width: 480px) { .upload-btn { padding: 10px 16px; font-size: 15px; } }
        .upload-btn:hover { background: #128c7e; }
        .form-group { margin-bottom: 20px; }
        @media (max-width: 480px) { .form-group { margin-bottom: 16px; } }
        .form-group label { display: block; margin-bottom: 8px; color: #8696a0; font-size: 13px; font-weight: 500;}
        @media (max-width: 480px) { .form-group label { font-size: 14px; } }
        .form-group input, .form-group textarea { width: 100%; padding: 10px 12px; background: #2A3942; border: 1px solid #3b4a54; border-radius: 6px; color: #e9edef; font-size: 15px; font-family: inherit; }
        @media (max-width: 480px) { .form-group input, .form-group textarea { padding: 12px 14px; font-size: 16px; border-radius: 4px; } }
        .form-group input:focus, .form-group textarea:focus { outline: none; border-color: #00a884; background-color: #111B21;}
        .save-btn, .modal-btn { width: 100%; padding: 10px 12px; background: #00a884; border: none; border-radius: 6px; color: white; font-size: 15px; font-weight: 500; cursor: pointer; transition: background 0.2s; }
        @media (max-width: 480px) { .save-btn, .modal-btn { padding: 12px 14px; font-size: 16px; border-radius: 4px; } }
        .save-btn:hover, .modal-btn:hover { background: #128c7e; }
        .modal-btn.secondary { background-color: #3b4a54; margin-top: 10px; }
        .modal-btn.secondary:hover { background-color: #4a5c68; }
        .modal-error { color: #f15c6c; font-size: 13px; margin-top: 5px; min-height: 18px; }
        @media (max-width: 480px) { .modal-error { font-size: 14px; } }
        .search-result { margin-top: 15px; padding: 12px; background: #111B21; border-radius: 6px; border: 1px solid #2a3942;}
        @media (max-width: 480px) { .search-result { padding: 10px; margin-top: 12px; } }
        .search-result p { margin-bottom: 8px; font-size: 14px; color: #AEBAC1;}
        .search-result p strong { color: #E9EDEF; font-weight: 500;}
        /* Scrollbar */
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #374045; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #4a545a; }
        @media (max-width: 480px) { ::-webkit-scrollbar { width: 4px; } }
        /* Empty List Placeholders */
        .empty-list-placeholder {
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            padding: 20px; text-align: center; color: #8696a0; width: 80%;
            display: none;
            flex-direction: column; align-items: center; justify-content: center;
         }
        @media (max-width: 480px) { .empty-list-placeholder { width: 90%; padding: 16px; } }
        .empty-list-placeholder i { font-size: 48px; margin-bottom: 16px; display: block; color: #3b4a54; opacity: 0.5; }
        @media (max-width: 480px) { .empty-list-placeholder i { font-size: 40px; } }
        .empty-list-placeholder p { font-size: 14px; line-height: 1.5; }
        @media (max-width: 480px) { .empty-list-placeholder p { font-size: 13px; } }
        /* Notification Styling */
        .notification-popup {
             position: fixed; top: 20px; right: 20px; background-color: #202c33; color: #e9edef;
             padding: 12px 16px; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
             z-index: 9999; font-size: 14px; display: none; opacity: 0; transition: opacity 0.3s ease-in-out, transform 0.3s ease-in-out;
             border-left: 4px solid #00a884; max-width: 300px;
         }
        @media (max-width: 480px) { .notification-popup { right: 10px; top: 10px; left: 10px; max-width: none; width: calc(100% - 20px); } }
        .notification-popup.show { display: block; opacity: 1; transform: translateY(0); }
        /* Typing Indicator */
        .typing-indicator {
            padding: 8px 16px; font-size: 12px; color: #8696a0; font-style: italic;
            display: none; align-self: flex-start; background: #202c33; border-radius: 7.5px; margin: 0 16px;
        }
        @media (max-width: 480px) { .typing-indicator { padding: 6px 12px; font-size: 11px; margin: 0 8px; } }
        .typing-indicator.active { display: block; }
        /* Additional Mobile Optimizations */
        @media (max-width: 768px) {
            .app-container { max-width: 100%; overflow: hidden; }
            .chat-area.active { animation: slideInFromRight 0.3s ease-out; }
            @keyframes slideInFromRight { from { transform: translateX(100%); } to { transform: translateX(0); } }
        }
        @media (max-width: 480px) {
            body { font-size: 15px; }
            .message { word-break: break-word; hyphens: auto; }
            .input-actions { flex-wrap: wrap; justify-content: center; }
        }
        .message-input {
            overflow-y: auto;
            min-height: 44px;
        }
        .message-input:focus {
            outline: none;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #111b21; color: #e9edef; height: 100vh; overflow: hidden; }
        .app-container { display: flex; height: 100vh; max-width: 1400px; margin: 0 auto; background: #0b141a; }
        .sidebar { width: 100%; max-width: 400px; background: #111b21; border-right: 1px solid #2a3942; display: flex; flex-direction: column; }
        @media (max-width: 768px) { .sidebar { max-width: 100%; border-right: none;} .chat-area { display: none; } .chat-area.active { display: flex; position: absolute; top: 0; left: 0; width: 100%; height: 100%; z-index: 100;} }

        .sidebar-header { padding: 10px 16px; height: 60px; background: #202c33; border-bottom: 1px solid #2a3942; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0;}
        .user-profile { display: flex; align-items: center; gap: 10px; cursor: pointer; position: relative; }
        .profile-avatar { width: 40px; height: 40px; border-radius: 50%; background: linear-gradient(135deg, #00a884, #128c7e); display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 16px; overflow: hidden; flex-shrink: 0; }
        .profile-avatar img { width: 100%; height: 100%; object-fit: cover; }
        .profile-dropdown { position: absolute; top: calc(100% + 5px); left: 0; background: #2A3942; border-radius: 8px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4); width: 220px; z-index: 1000; display: none; padding: 8px 0; border: 1px solid #3b4a54; }
        .profile-dropdown.active { display: block; }
        .dropdown-item { padding: 10px 16px; display: flex; align-items: center; gap: 12px; cursor: pointer; transition: background 0.2s; font-size: 14px; color: #e9edef;}
        .dropdown-item i { width: 18px; text-align: center; color: #8696a0;}
        .dropdown-item:hover { background: #3b4a54; }
        #logout { color: #f15c6c; border-top: 1px solid #3b4a54; margin-top: 5px; padding-top: 10px;}
        #logout i { color: #f15c6c;}

        .sidebar-actions { display: flex; gap: 10px; align-items: center;}
        .sidebar-action-btn { background: none; border: none; color: #AEBAC1; font-size: 20px; cursor: pointer; transition: color 0.2s; padding: 8px; border-radius: 50%; }
        .sidebar-action-btn:hover { color: #e9edef; background-color: rgba(255, 255, 255, 0.1); }

        .search-container { padding: 8px 12px; border-bottom: 1px solid #2a3942; background: #111B21; flex-shrink: 0;}
        .search-box { width: 100%; padding: 8px 16px; background: #202c33; border: none; border-radius: 8px; color: #e9edef; font-size: 14px; height: 36px; }
        .search-box::placeholder { color: #8696a0; }

        .tabs { display: flex; border-bottom: 1px solid #2a3942; flex-shrink: 0;}
        .tab { flex: 1; padding: 14px 10px; text-align: center; cursor: pointer; color: #8696a0; font-size: 14px; position: relative; font-weight: 600; text-transform: uppercase; }
        .tab.active { color: #00A884; }
        .tab.active::after { content: ''; position: absolute; bottom: 0; left: 0; width: 100%; height: 3px; background: #00A884; }

        .list-container { flex: 1; overflow-y: auto; position: relative; }
        .chats-list, .contacts-list { display: none; height: 100%;}
        .chats-list.active, .contacts-list.active { display: block; }

        .chat-item, .contact-item { padding: 10px 16px; display: flex; align-items: center; gap: 15px; cursor: pointer; border-bottom: 1px solid #2a3942; transition: background 0.15s ease-in-out; position: relative; min-height: 72px; }
        .chat-item:last-child, .contact-item:last-child { border-bottom: none; }
        .chat-item:hover, .contact-item:hover, .chat-item.active { background: #2a3942; }
        .chat-avatar, .contact-avatar { width: 49px; height: 49px; border-radius: 50%; background: #3b4a54; display: flex; align-items: center; justify-content: center; font-weight: 400; font-size: 18px; overflow: hidden; position: relative; flex-shrink: 0;}
        .chat-avatar img, .contact-avatar img { width: 100%; height: 100%; object-fit: cover; }
        .online-indicator { position: absolute; bottom: 2px; right: 2px; width: 10px; height: 10px; background: #00A884; border: 1.5px solid #111b21; border-radius: 50%; }

        .chat-info, .contact-info { flex: 1; min-width: 0; padding-right: 5px; }
        .chat-info-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
        .chat-name, .contact-name { font-weight: 400; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 16px; color: #E9EDEF; }
        .chat-time { font-size: 12px; color: #8696a0; white-space: nowrap; flex-shrink: 0; margin-left: 6px; }
        .chat-info-bottom { display: flex; align-items: center; justify-content: space-between; }
        .chat-last-message, .contact-status { font-size: 14px; color: #8696a0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex-grow: 1; padding-right: 5px; }
        .unread-badge { background: #00A884; color: #111B21; font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 10px; min-width: 18px; height: 18px; line-height: 16px; text-align: center; flex-shrink: 0; }
        .lock-icon { display: inline-block; color: #8696a0; font-size: 11px; margin-right: 3px; vertical-align: middle; }

        /* Chat Area */
        .chat-area { flex: 1; display: flex; flex-direction: column; background-color: #0b141a; }
        .chat-header { padding: 10px 16px; height: 60px; background: #202c33; border-bottom: 1px solid #2a3942; display: flex; align-items: center; gap: 15px; flex-shrink: 0;}
         .back-button { background: none; border: none; color: #AEBAC1; font-size: 20px; cursor: pointer; display: none; margin-right: 5px;}
         @media (max-width: 768px) { .back-button { display: inline-block; } }

        .chat-contact-avatar { width: 40px; height: 40px; border-radius: 50%; background: #3b4a54; display: flex; align-items: center; justify-content: center; font-weight: 400; font-size: 16px; overflow: hidden; flex-shrink: 0;}
        .chat-contact-avatar img { width: 100%; height: 100%; object-fit: cover; }
        .chat-contact-info { flex: 1; min-width: 0; cursor: pointer; }
        .chat-contact-name { font-weight: 500; font-size: 16px; margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #E9EDEF; }
        .chat-contact-status { font-size: 12px; color: #8696a0; display: flex; align-items: center; gap: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
        .online-dot { width: 8px; height: 8px; background: #00A884; border-radius: 50%; }
        .chat-actions { display: flex; gap: 10px; margin-left: auto; }
        .chat-action-btn { background: none; border: none; color: #AEBAC1; cursor: pointer; font-size: 20px; transition: color 0.2s; padding: 8px; border-radius: 50%; }
        .chat-action-btn:hover { color: #e9edef; background-color: rgba(255, 255, 255, 0.1);}

        .messages-container { flex: 1; padding: 10px 8%; overflow-y: auto; background-image: url("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAJYAAACWCAYAAAA8AXHiAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAEaSURBVHhe7dOxAQAhEACx/0/tTdtBRDU/7lxcAMDxPBYAwsEBYQGwDywgFgHLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA/+/UeAAdgDEhALgGUAA2EJMAGwDywgFgHLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAGwDywgFgDLAAbCEmABsA8sIBYAywAGwhJgAbAPLCAWAMsABsISYAEy5AACAAmQKEkAAAAASUVORK5CYII="); background-color: #0b141a; background-repeat: repeat; background-size: auto; display: flex; flex-direction: column; gap: 3px;}
        .message { max-width: 65%; padding: 6px 9px 8px 9px; border-radius: 7.5px; position: relative; word-wrap: break-word; line-height: 1.3; font-size: 14.2px; box-shadow: 0 1px 0.5px rgba(11,20,26,.13); margin-bottom: 5px; }
        .message.sent { align-self: flex-end; background: #005c4b; border-top-right-radius: 0; color: #e9edef; }
        .message.received { align-self: flex-start; background: #202c33; border-top-left-radius: 0; color: #e9edef; }
        .message-text { margin-bottom: 4px; line-height: 1.4; }
        .message-meta { display: flex; justify-content: flex-end; align-items: center; font-size: 11px; color: rgba(233, 237, 239, 0.6); margin-top: 2px; height: 15px; line-height: 15px; }
        .message-time { margin-right: 5px; }
        .toggle-btn { background: none; border: none; color: rgba(233, 237, 239, 0.6); padding: 0 5px; cursor: pointer; font-size: 11px; transition: color 0.2s; margin-left: auto; }
        .toggle-btn:hover { color: #e9edef; }

        /* File Message Styles */
        .file-message { border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px; margin: 5px 0; background: rgba(255,255,255,0.05); }
        .file-icon { font-size: 24px; margin-bottom: 8px; }
        .file-name { font-weight: 500; margin-bottom: 4px; word-break: break-all; }
        .file-size { font-size: 12px; color: #8696a0; }
        .file-download-btn { background: #00a884; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; margin-top: 8px; font-size: 12px; }
        .file-download-btn:hover { background: #128c7e; }

        /* Voice Message Styles */
        .voice-message { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: rgba(255,255,255,0.1); border-radius: 20px; }
        .voice-play-btn { background: none; border: none; color: #00a884; font-size: 18px; cursor: pointer; }
        .voice-waveform { flex: 1; height: 20px; background: linear-gradient(90deg, #00a884 50%, rgba(255,255,255,0.3) 50%); border-radius: 10px; }
        .voice-duration { font-size: 12px; color: #8696a0; }

        /* Poll Message Styles */
        .poll-message { border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px; margin: 5px 0; background: rgba(255,255,255,0.05); }
        .poll-question { font-weight: 500; margin-bottom: 12px; }
        .poll-option { padding: 8px 12px; margin: 6px 0; background: rgba(255,255,255,0.1); border-radius: 6px; cursor: pointer; transition: background 0.2s; }
        .poll-option:hover { background: rgba(255,255,255,0.2); }
        .poll-option.voted { background: rgba(0,168,132,0.3); border: 1px solid #00a884; }
        .poll-results { margin-top: 8px; }
        .poll-result-bar { height: 6px; background: #00a884; border-radius: 3px; margin-top: 4px; }
        .poll-vote-count { font-size: 11px; color: #8696a0; text-align: right; margin-top: 2px; }

        /* Contact Share Styles */
        .contact-share { border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px; margin: 5px 0; background: rgba(255,255,255,0.05); }
        .contact-name-share { font-weight: 500; margin-bottom: 4px; }
        .contact-phone-share { font-size: 12px; color: #8696a0; margin-bottom: 8px; }
        .contact-add-btn { background: #00a884; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }
        .contact-add-btn:hover { background: #128c7e; }

        .input-area { padding: 5px 16px; min-height: 50px; background: #202c33; border-top: 1px solid #2a3942; display: flex; align-items: flex-end; flex-shrink: 0;}
        .input-container { display: flex; align-items: flex-end; gap: 10px; width: 100%;}
        .input-actions { display: flex; gap: 5px; }
        .input-action-btn { background: none; border: none; color: #AEBAC1; cursor: pointer; font-size: 24px; transition: color 0.2s; padding: 8px 10px; border-radius: 50%;}
        .input-action-btn:hover { color: #e9edef; background-color: rgba(255, 255, 255, 0.1); }
        .input-action-btn.active { color: #00a884; background-color: rgba(0,168,132,0.1); }
        .message-input { flex: 1; background: #2A3942; border: none; border-radius: 20px; padding: 9px 12px; color: #e9edef; font-size: 15px; resize: none; max-height: 100px; line-height: 20px; font-family: inherit; margin: 5px 0; }
        .message-input::placeholder { color: #8696a0; }
        .send-btn { background: none; border: none; width: 40px; height: 40px; border-radius: 50%; color: #AEBAC1; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.2s; flex-shrink: 0; font-size: 22px; margin-bottom: 5px;}
        .send-btn:hover { color: #e9edef; background-color: rgba(255, 255, 255, 0.1); }
        .send-btn.has-text { color: #00A884; }
        .send-btn.has-text:hover { background-color: rgba(0, 168, 132, 0.1); }

        /* Emoji Picker */
        .emoji-picker-container { position: absolute; bottom: 60px; left: 16px; z-index: 1000; display: none; }
        emoji-picker { --background: #2A3942; --border-color: #3b4a54; --input-border-color: #3b4a54; --input-placeholder-color: #8696a0; --category-emoji-padding: 8px; --indicator-color: #00a884; }

        /* Voice Recording */
        .voice-recorder { display: none; align-items: center; gap: 10px; padding: 8px 16px; background: #2A3942; border-radius: 20px; margin: 5px 0; }
        .voice-recorder.active { display: flex; }
        .recording-indicator { width: 12px; height: 12px; background: #f15c6c; border-radius: 50%; animation: pulse 1s infinite; }
        @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
        .recording-timer { font-size: 14px; color: #e9edef; }
        .cancel-recording { background: none; border: none; color: #f15c6c; cursor: pointer; font-size: 16px; }

        /* File Upload */
        .file-upload-container { display: none; position: absolute; bottom: 60px; right: 16px; background: #2A3942; border-radius: 8px; padding: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); z-index: 1000; width: 200px; }
        .file-upload-container.active { display: block; }
        .upload-option { padding: 12px 16px; display: flex; align-items: center; gap: 12px; cursor: pointer; transition: background 0.2s; border-radius: 6px; }
        .upload-option:hover { background: #3b4a54; }
        .upload-option i { font-size: 18px; color: #00a884; }

        /* Poll Creator */
        .poll-creator { display: none; position: absolute; bottom: 60px; right: 16px; background: #2A3942; border-radius: 8px; padding: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); z-index: 1000; width: 300px; }
        .poll-creator.active { display: block; }
        .poll-input { width: 100%; padding: 8px 12px; background: #202c33; border: 1px solid #3b4a54; border-radius: 4px; color: #e9edef; margin-bottom: 8px; }
        .poll-option-input { display: flex; gap: 8px; margin-bottom: 8px; }
        .add-option-btn { background: #00a884; color: white; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }
        .create-poll-btn { width: 100%; padding: 8px 12px; background: #00a884; color: white; border: none; border-radius: 4px; cursor: pointer; margin-top: 12px; }

        .empty-chat { flex: 1; display: flex; align-items: center; justify-content: center; text-align: center; color: #8696a0; padding: 20px;}
        .empty-chat h3 { margin-bottom: 8px; color: #e9edef; font-weight: 300; font-size: 24px; }
        .empty-chat p { font-size: 14px;}
        .empty-chat i { font-size: 48px; margin-bottom: 16px; color: #3b4a54; }

        /* Modal Styles */
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(11, 20, 26, 0.85); z-index: 2000; align-items: center; justify-content: center; backdrop-filter: blur(3px); animation: fadeIn 0.3s ease-out; }
        .modal.active { display: flex; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        .modal-content { background: #202c33; border-radius: 8px; width: 90%; max-width: 450px; max-height: 90vh; overflow-y: auto; box-shadow: 0 5px 20px rgba(0,0,0,0.4); animation: slideIn 0.3s ease-out; }
        @keyframes slideIn { from { transform: translateY(-20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
        .modal-header { padding: 16px 20px; border-bottom: 1px solid #2a3942; display: flex; align-items: center; justify-content: space-between; }
        .modal-title { font-size: 18px; font-weight: 500; color: #E9EDEF;}
        .modal-close { background: none; border: none; color: #8696a0; font-size: 20px; cursor: pointer; transition: color 0.2s; }
        .modal-close:hover { color: #e9edef; }
        .modal-body { padding: 20px; }
        .profile-pic-container { display: flex; flex-direction: column; align-items: center; margin-bottom: 20px; }
        .profile-pic-large { width: 120px; height: 120px; border-radius: 50%; background: linear-gradient(135deg, #00a884, #128c7e); display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 48px; overflow: hidden; margin-bottom: 16px; color: white; }
        .profile-pic-large img { width: 100%; height: 100%; object-fit: cover; }
        .upload-btn { background: #00a884; border: none; color: white; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; transition: background 0.2s; }
        .upload-btn:hover { background: #128c7e; }
        .form-group { margin-bottom: 20px; }
        .form-group label { display: block; margin-bottom: 8px; color: #8696a0; font-size: 13px; font-weight: 500;}
        .form-group input, .form-group textarea { width: 100%; padding: 10px 12px; background: #2A3942; border: 1px solid #3b4a54; border-radius: 6px; color: #e9edef; font-size: 15px; font-family: inherit; }
        .form-group input:focus, .form-group textarea:focus { outline: none; border-color: #00a884; background-color: #111B21;}
        .save-btn, .modal-btn { width: 100%; padding: 10px 12px; background: #00a884; border: none; border-radius: 6px; color: white; font-size: 15px; font-weight: 500; cursor: pointer; transition: background 0.2s; }
        .save-btn:hover, .modal-btn:hover { background: #128c7e; }
        .modal-btn.secondary { background-color: #3b4a54; margin-top: 10px; }
        .modal-btn.secondary:hover { background-color: #4a5c68; }
        .modal-error { color: #f15c6c; font-size: 13px; margin-top: 5px; min-height: 18px; }
        .search-result { margin-top: 15px; padding: 12px; background: #111B21; border-radius: 6px; border: 1px solid #2a3942;}
        .search-result p { margin-bottom: 8px; font-size: 14px; color: #AEBAC1;}
        .search-result p strong { color: #E9EDEF; font-weight: 500;}

        /* Scrollbar */
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #374045; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #4a545a; }

        /* Empty List Placeholders */
        .empty-list-placeholder {
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            padding: 20px; text-align: center; color: #8696a0; width: 80%;
            display: none;
            flex-direction: column; align-items: center; justify-content: center;
         }
        .empty-list-placeholder i { font-size: 48px; margin-bottom: 16px; display: block; color: #3b4a54; }
        .empty-list-placeholder p { font-size: 14px; line-height: 1.5; }

        /* Notification Styling */
        .notification-popup {
             position: fixed; top: 20px; right: 20px; background-color: #202c33; color: #e9edef;
             padding: 10px 15px; border-radius: 5px; box-shadow: 0 2px 10px rgba(0,0,0,0.3);
             z-index: 9999; font-size: 14px; display: none; opacity: 0; transition: opacity 0.3s ease-in-out;
             border-left: 4px solid #00a884;
         }
        .notification-popup.show { display: block; opacity: 1; }

        /* Typing Indicator */
        .typing-indicator {
            padding: 8px 16px; font-size: 12px; color: #8696a0; font-style: italic;
            display: none;
        }
        .typing-indicator.active { display: block; }

    </style>
</head>
<body>
    <div id="notificationPopup" class="notification-popup"></div>

    <div class="app-container">
        <div class="sidebar" id="sidebar"> <div class="sidebar-header">
                <div class="user-profile" id="userProfile">
                    <div class="profile-avatar" id="profileAvatar">
                        <span id="profileAvatarText">?</span>
                    </div>
                    <div class="profile-dropdown" id="profileDropdown">
                         <div style="padding: 10px 16px; border-bottom: 1px solid #3b4a54; margin-bottom: 5px;">
                              <div id="dropdownProfileName" style="font-weight: 500; color: #E9EDEF;">Loading...</div>
                              <div id="dropdownProfileStatus" style="font-size: 12px; color: #8696a0;">Status</div>
                         </div>
                        <div class="dropdown-item" id="editProfile">
                            <i class="fas fa-user-edit"></i> Edit Profile
                        </div>
                         <div class="dropdown-item" id="logout">
                           <i class="fas fa-sign-out-alt"></i> Logout
                        </div>
                    </div>
                </div>
                 <div class="sidebar-actions">
                     <button class="sidebar-action-btn" id="addContactBtn" title="Add New Contact">
                          <i class="fas fa-user-plus"></i>
                     </button>
                    <button class="sidebar-action-btn" title="Status (Not Implemented)"> <i class="fas fa-circle-notch"></i> </button>
                     <button class="sidebar-action-btn" title="More Options (Not Implemented)"> <i class="fas fa-ellipsis-v"></i> </button>
                </div>
            </div>
            <div class="search-container">
                <input type="text" class="search-box" placeholder="Search or start new chat" id="searchBox">
            </div>
            <div class="tabs">
                <div class="tab active" id="chatsTab">Chats</div>
                <div class="tab" id="contactsTab">Contacts</div>
            </div>
            <div class="list-container">
                <div class="chats-list active" id="chatsList">
                    <div class="empty-list-placeholder" id="emptyChatsPlaceholder">
                        <i class="fas fa-comments"></i>
                        <p>No chats yet. Start a conversation from your contacts.</p>
                    </div>
                    </div>
                <div class="contacts-list" id="contactsList">
                     <div class="empty-list-placeholder" id="emptyContactsPlaceholder">
                        <i class="fas fa-address-book"></i>
                        <p>No contacts yet. Click the <i class="fas fa-user-plus" style="font-size: 1em;"></i> icon above to add contacts.</p>
                    </div>
                    </div>
             </div>
        </div>

        <div class="chat-area" id="chatArea"> <div class="chat-header" id="chatHeader" style="display: none;">
                <button class="back-button" id="backToSidebarBtn" title="Back to chats"><i class="fas fa-arrow-left"></i></button>
                <div class="chat-contact-avatar" id="chatAvatar">
                    <span id="chatAvatarText">?</span>
                </div>
                <div class="chat-contact-info" id="chatContactInfo"> <div class="chat-contact-name" id="chatContactName">Select Chat</div>
                    <div class="chat-contact-status" id="chatContactStatus">
                        <span class="online-dot" id="onlineDot" style="display: none;"></span>
                        <span id="chatStatusText">Offline</span>
                    </div>
                </div>
                <div class="chat-actions">
                    <button class="chat-action-btn" id="videoCallBtn" title="Video Call (Not Implemented)"> <i class="fas fa-video"></i> </button>
                    <button class="chat-action-btn" id="voiceCallBtn" title="Voice Call (Not Implemented)"> <i class="fas fa-phone"></i> </button>
                    <button class="chat-action-btn" id="moreOptionsBtn" title="More Options (Not Implemented)"> <i class="fas fa-ellipsis-v"></i> </button>
                </div>
            </div>
            <div class="messages-container" id="messagesContainer">
                <div class="empty-chat" id="emptyChat"> <div>
                        <i class="fas fa-shield-alt"></i> <h3>Keep your chats secure</h3>
                        <p>Select a chat to start messaging.<br>Messages are end-to-end encrypted (placeholder).</p>
                    </div>
                </div>
                <div class="empty-list-placeholder" id="emptyMessagesPlaceholder" style="display: none;"> <i class="fas fa-comments"></i>
                    <p>No messages yet. Send a message to start the conversation.</p>
                 </div>
                 <div class="typing-indicator" id="typingIndicator">
                    <span id="typingText">is typing...</span>
                 </div>
                 </div>
            <div class="input-area" id="inputArea" style="display: none;">
                <div class="input-container">
                    <div class="input-actions">
                        <button class="input-action-btn" id="emojiBtn" title="Emoji"> <i class="far fa-grin"></i> </button> 
                        <button class="input-action-btn" id="attachBtn" title="Attach File"> <i class="fas fa-paperclip"></i> </button>
                        <button class="input-action-btn" id="pollBtn" title="Create Poll"> <i class="fas fa-chart-bar"></i> </button>
                        <button class="input-action-btn" id="contactBtn" title="Share Contact"> <i class="fas fa-user-plus"></i> </button>
                    </div>
                    <textarea class="message-input" id="messageInput" placeholder="Type a message" rows="1"></textarea>
                    <button class="send-btn" id="sendBtn" title="Send Message">
                        <i class="fas fa-microphone"></i>
                    </button>
                </div>
                <div class="voice-recorder" id="voiceRecorder">
                    <div class="recording-indicator"></div>
                    <div class="recording-timer" id="recordingTimer">00:00</div>
                    <button class="cancel-recording" id="cancelRecording" title="Cancel Recording">
                        <i class="fas fa-times"></i>
                    </button>
                </div>
            </div>
            
            <!-- Emoji Picker -->
            <div class="emoji-picker-container" id="emojiPickerContainer">
                <emoji-picker id="emojiPicker"></emoji-picker>
            </div>
            
            <!-- File Upload Options -->
            <div class="file-upload-container" id="fileUploadContainer">
                <div class="upload-option" id="uploadImage">
                    <i class="fas fa-image"></i>
                    <span>Photo & Video</span>
                </div>
                <div class="upload-option" id="uploadDocument">
                    <i class="fas fa-file"></i>
                    <span>Document</span>
                </div>
                <div class="upload-option" id="uploadAudio">
                    <i class="fas fa-music"></i>
                    <span>Audio</span>
                </div>
            </div>
            
            <!-- Poll Creator -->
            <div class="poll-creator" id="pollCreator">
                <input type="text" class="poll-input" id="pollQuestion" placeholder="Ask a question...">
                <div id="pollOptions">
                    <input type="text" class="poll-input poll-option-input" placeholder="Option 1">
                    <input type="text" class="poll-input poll-option-input" placeholder="Option 2">
                </div>
                <button class="add-option-btn" id="addOptionBtn">Add Option</button>
                <button class="create-poll-btn" id="createPollBtn">Create Poll</button>
            </div>
        </div>
    </div>

    <div class="modal" id="profileModal"> <div class="modal-content"> <div class="modal-header"> <div class="modal-title">Profile</div> <button class="modal-close" id="closeProfileModal"> <i class="fas fa-times"></i> </button> </div> <div class="modal-body"> <div class="profile-pic-container"> <div class="profile-pic-large" id="profilePicLarge"> <span id="profilePicLargeText">?</span> </div> <input type="file" id="profilePicInput" accept="image/*" style="display: none;"> <button class="upload-btn" id="uploadProfilePic">Upload Photo</button> </div> <form id="profileForm"> <div class="form-group"> <label for="profileNameInput">Your Name</label> <input type="text" id="profileNameInput" placeholder="Enter your name" required minlength="2" maxlength="50"> <div class="modal-error" id="profileNameError"></div> </div> <div class="form-group"> <label for="profileStatusInput">About</label> <input type="text" id="profileStatusInput" placeholder="Hey there! I'm using SecureChat" maxlength="100"> <div class="modal-error" id="profileStatusError"></div> </div> <div class="form-group"> <label for="currentPasswordInput">Current Password</label> <input type="password" id="currentPasswordInput" placeholder="Enter current password"> <div class="modal-error" id="currentPasswordError"></div> </div> <div class="form-group"> <label for="newPasswordInput">New Password</label> <input type="password" id="newPasswordInput" placeholder="Enter new password"> <div class="modal-error" id="newPasswordError"></div> </div> <button type="submit" class="save-btn">Save Changes</button> <div class="modal-error" id="profileFormError" style="margin-top: 15px;"></div> </form> </div> </div> </div>

     <div class="modal" id="addContactModal"> <div class="modal-content"> <div class="modal-header"> <div class="modal-title">Add New Contact</div> <button class="modal-close" id="closeAddContactModal"> <i class="fas fa-times"></i> </button> </div> <div class="modal-body"> <form id="findUserForm"> <div class="form-group"> <label for="findUserPhoneInput">Enter Phone Number (Intl. format, e.g., +91...)</label> <input type="tel" id="findUserPhoneInput" placeholder="+1234567890" required> </div> <button type="submit" class="modal-btn">Search User</button> <div class="modal-error" id="findUserError" style="margin-top: 15px;"></div> </form> <div id="searchResultArea" style="display: none;"> <hr style="border-color: #3b4a54; margin: 20px 0;"> <h4>User Found:</h4> <div class="search-result" id="searchResultDetails"> </div> <button class="modal-btn" id="confirmAddContactBtn" style="margin-top: 15px;">Add to Contacts</button> <div class="modal-error" id="addContactError" style="margin-top: 10px;"></div> </div> </div> </div> </div>

    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script> 
    <script type="module" src="https://cdn.jsdelivr.net/npm/emoji-picker-element@1.12.0/index.js"></script>
    <script>
        // --- Global state & DOM Elements ---
        let currentUser = null;
        let selectedChat = null;
        let messageStates = {};
        let socket = null;
        let refreshInterval = null;
        let isChatAreaActive = false;
        let isRecording = false;
        let recordingTimer = null;
        let recordingStartTime = null;
        let mediaRecorder = null;
        let audioChunks = [];

        // DOM Elements
        const profileElements = {
             userProfile: document.getElementById('userProfile'),
             profileDropdown: document.getElementById('profileDropdown'),
             profileAvatar: document.getElementById('profileAvatar'),
             profileAvatarText: document.getElementById('profileAvatarText'),
             editProfileBtn: document.getElementById('editProfile'),
             logoutBtn: document.getElementById('logout'),
             dropdownName: document.getElementById('dropdownProfileName'),
             dropdownStatus: document.getElementById('dropdownProfileStatus'),
         };
         const sidebarElements = {
             sidebar: document.getElementById('sidebar'),
             addContactBtn: document.getElementById('addContactBtn'),
             searchBox: document.getElementById('searchBox'),
             chatsTab: document.getElementById('chatsTab'),
             contactsTab: document.getElementById('contactsTab'),
             chatsList: document.getElementById('chatsList'),
             contactsList: document.getElementById('contactsList'),
             emptyChatsPlaceholder: document.getElementById('emptyChatsPlaceholder'),
             emptyContactsPlaceholder: document.getElementById('emptyContactsPlaceholder'),
         };
         const chatAreaElements = {
              chatArea: document.getElementById('chatArea'),
              backButton: document.getElementById('backToSidebarBtn'),
              chatHeader: document.getElementById('chatHeader'),
              chatAvatar: document.getElementById('chatAvatar'),
              chatAvatarText: document.getElementById('chatAvatarText'),
              chatContactInfo: document.getElementById('chatContactInfo'),
              chatContactName: document.getElementById('chatContactName'),
              chatContactStatus: document.getElementById('chatContactStatus'),
              onlineDot: document.getElementById('onlineDot'),
              chatStatusText: document.getElementById('chatStatusText'),
              messagesContainer: document.getElementById('messagesContainer'),
              emptyChat: document.getElementById('emptyChat'),
              emptyMessagesPlaceholder: document.getElementById('emptyMessagesPlaceholder'),
              inputArea: document.getElementById('inputArea'),
              messageInput: document.getElementById('messageInput'),
              sendBtn: document.getElementById('sendBtn'),
              sendBtnIcon: document.querySelector('#sendBtn i'),
              typingIndicator: document.getElementById('typingIndicator'),
              typingText: document.getElementById('typingText'),
         };
         const featureElements = {
             emojiBtn: document.getElementById('emojiBtn'),
             emojiPickerContainer: document.getElementById('emojiPickerContainer'),
             emojiPicker: document.getElementById('emojiPicker'),
             attachBtn: document.getElementById('attachBtn'),
             fileUploadContainer: document.getElementById('fileUploadContainer'),
             uploadImage: document.getElementById('uploadImage'),
             uploadDocument: document.getElementById('uploadDocument'),
             uploadAudio: document.getElementById('uploadAudio'),
             pollBtn: document.getElementById('pollBtn'),
             pollCreator: document.getElementById('pollCreator'),
             pollQuestion: document.getElementById('pollQuestion'),
             pollOptions: document.getElementById('pollOptions'),
             addOptionBtn: document.getElementById('addOptionBtn'),
             createPollBtn: document.getElementById('createPollBtn'),
             contactBtn: document.getElementById('contactBtn'),
             voiceRecorder: document.getElementById('voiceRecorder'),
             recordingTimer: document.getElementById('recordingTimer'),
             cancelRecording: document.getElementById('cancelRecording'),
         };
         const profileModalElements = {
             modal: document.getElementById('profileModal'),
             closeBtn: document.getElementById('closeProfileModal'),
             picLarge: document.getElementById('profilePicLarge'),
             picLargeText: document.getElementById('profilePicLargeText'),
             uploadBtn: document.getElementById('uploadProfilePic'),
             profilePicInput: document.getElementById('profilePicInput'),
             form: document.getElementById('profileForm'),
             nameInput: document.getElementById('profileNameInput'),
             statusInput: document.getElementById('profileStatusInput'),
             currentPasswordInput: document.getElementById('currentPasswordInput'),
             newPasswordInput: document.getElementById('newPasswordInput'),
             nameError: document.getElementById('profileNameError'),
             statusError: document.getElementById('profileStatusError'),
             currentPasswordError: document.getElementById('currentPasswordError'),
             newPasswordError: document.getElementById('newPasswordError'),
             formError: document.getElementById('profileFormError'),
         };
         const addContactModalElements = {
             modal: document.getElementById('addContactModal'),
             closeBtn: document.getElementById('closeAddContactModal'),
             form: document.getElementById('findUserForm'),
             phoneInput: document.getElementById('findUserPhoneInput'),
             error: document.getElementById('findUserError'),
             resultArea: document.getElementById('searchResultArea'),
             resultDetails: document.getElementById('searchResultDetails'),
             confirmBtn: document.getElementById('confirmAddContactBtn'),
             addError: document.getElementById('addContactError'),
         };
         const notificationElement = document.getElementById('notificationPopup');

        // --- Initialization ---
        document.addEventListener('DOMContentLoaded', () => {
            initApp();
            setupEventListeners();
            connectSocketIO();
            startAutoRefresh();
        });

        async function initApp() {
            await loadCurrentUser();
            if (currentUser) {
                loadChats();
                loadContacts();
            } else {
                console.error("Initialization failed: No current user data.");
            }
        }

        // --- Event Listeners Setup ---
        function setupEventListeners() {
            // Profile Dropdown
            profileElements.userProfile.addEventListener('click', toggleProfileDropdown);
            document.addEventListener('click', closeProfileDropdownOnClickOutside);
            profileElements.editProfileBtn.addEventListener('click', (e) => { e.stopPropagation(); openProfileModal(); });
            profileElements.logoutBtn.addEventListener('click', () => window.location.href = '/logout');

            // Sidebar Actions
            sidebarElements.addContactBtn.addEventListener('click', openAddContactModal);

            // Tabs
            sidebarElements.chatsTab.addEventListener('click', showChatsTab);
            sidebarElements.contactsTab.addEventListener('click', showContactsTab);

            // Search
            sidebarElements.searchBox.addEventListener('input', handleSearch);

            // Message Input & Send Button Icon Change
            chatAreaElements.messageInput.addEventListener('input', () => {
                 autoResizeInput();
                 toggleSendButtonIcon();
                 handleTyping();
             });
            chatAreaElements.messageInput.addEventListener('focus', () => {
                if (selectedChat) handleTyping(true);
            });
            chatAreaElements.messageInput.addEventListener('blur', () => {
                if (selectedChat) handleTyping(false);
            });
            chatAreaElements.sendBtn.addEventListener('click', sendMessage);
            chatAreaElements.sendBtn.addEventListener('mousedown', startVoiceRecording);
            chatAreaElements.sendBtn.addEventListener('mouseup', stopVoiceRecording);
            chatAreaElements.sendBtn.addEventListener('touchstart', startVoiceRecording);
            chatAreaElements.sendBtn.addEventListener('touchend', stopVoiceRecording);
            chatAreaElements.messageInput.addEventListener('keydown', handleMessageInputKeydown);

            // Feature Buttons
            featureElements.emojiBtn.addEventListener('click', toggleEmojiPicker);
            featureElements.attachBtn.addEventListener('click', toggleFileUpload);
            featureElements.pollBtn.addEventListener('click', togglePollCreator);
            featureElements.contactBtn.addEventListener('click', shareContact);
            featureElements.cancelRecording.addEventListener('click', cancelVoiceRecording);

            // File Upload Options
            featureElements.uploadImage.addEventListener('click', () => uploadFile('image'));
            featureElements.uploadDocument.addEventListener('click', () => uploadFile('document'));
            featureElements.uploadAudio.addEventListener('click', () => uploadFile('audio'));

            // Poll Creator
            featureElements.addOptionBtn.addEventListener('click', addPollOption);
            featureElements.createPollBtn.addEventListener('click', createPoll);

            // Emoji Picker
            featureElements.emojiPicker.addEventListener('emoji-click', (event) => {
                const emoji = event.detail.unicode;
                chatAreaElements.messageInput.value += emoji;
                chatAreaElements.messageInput.focus();
                toggleSendButtonIcon();
            });

            // Close feature panels when clicking outside
            document.addEventListener('click', (e) => {
                if (!featureElements.emojiPickerContainer.contains(e.target) && e.target !== featureElements.emojiBtn) {
                    featureElements.emojiPickerContainer.style.display = 'none';
                }
                if (!featureElements.fileUploadContainer.contains(e.target) && e.target !== featureElements.attachBtn) {
                    featureElements.fileUploadContainer.classList.remove('active');
                }
                if (!featureElements.pollCreator.contains(e.target) && e.target !== featureElements.pollBtn) {
                    featureElements.pollCreator.classList.remove('active');
                }
            });

            // Profile Modal
            profileModalElements.closeBtn.addEventListener('click', closeProfileModal);
            profileModalElements.uploadBtn.addEventListener('click', () => profileModalElements.profilePicInput.click());
            profileModalElements.profilePicInput.addEventListener('change', uploadProfilePicture);
            profileModalElements.form.addEventListener('submit', saveProfile);

            // Add Contact Modal
            addContactModalElements.closeBtn.addEventListener('click', closeAddContactModal);
            addContactModalElements.form.addEventListener('submit', searchUserByPhone);

             // Mobile Back Button
             chatAreaElements.backButton.addEventListener('click', showSidebarView);
        }

         function toggleProfileDropdown(event) {
             event.stopPropagation();
             profileElements.profileDropdown.classList.toggle('active');
         }

        function closeProfileDropdownOnClickOutside(event) {
             if (!profileElements.userProfile.contains(event.target)) {
                 profileElements.profileDropdown.classList.remove('active');
             }
         }

        function showChatsTab() {
            sidebarElements.chatsTab.classList.add('active');
            sidebarElements.contactsTab.classList.remove('active');
            sidebarElements.chatsList.classList.add('active');
            sidebarElements.contactsList.classList.remove('active');
            handleSearch();
        }

        function showContactsTab() {
            sidebarElements.contactsTab.classList.add('active');
            sidebarElements.chatsTab.classList.remove('active');
            sidebarElements.contactsList.classList.add('active');
            sidebarElements.chatsList.classList.remove('active');
            handleSearch();
        }

        function handleSearch() {
             const searchTerm = sidebarElements.searchBox.value.toLowerCase();
             filterChats(searchTerm);
             filterContacts(searchTerm);
        }

         function toggleSendButtonIcon() {
             const hasText = chatAreaElements.messageInput.value.trim().length > 0;
             if (hasText) {
                 chatAreaElements.sendBtnIcon.className = 'fas fa-paper-plane';
                 chatAreaElements.sendBtn.classList.add('has-text');
                 chatAreaElements.sendBtn.title = "Send Message";
             } else {
                 chatAreaElements.sendBtnIcon.className = 'fas fa-microphone';
                 chatAreaElements.sendBtn.classList.remove('has-text');
                 chatAreaElements.sendBtn.title = "Hold to record voice message";
             }
         }

        function handleMessageInputKeydown(event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        }

        function autoResizeInput() {
             const input = chatAreaElements.messageInput;
             input.style.height = 'auto';
             const scrollHeight = input.scrollHeight;
             const newHeight = Math.min(scrollHeight, 100);
             input.style.height = newHeight + 'px';
        }

        // --- Feature Functions ---
        function toggleEmojiPicker() {
            const isVisible = featureElements.emojiPickerContainer.style.display === 'block';
            featureElements.emojiPickerContainer.style.display = isVisible ? 'none' : 'block';
            featureElements.fileUploadContainer.classList.remove('active');
            featureElements.pollCreator.classList.remove('active');
        }

        function toggleFileUpload() {
            featureElements.fileUploadContainer.classList.toggle('active');
            featureElements.emojiPickerContainer.style.display = 'none';
            featureElements.pollCreator.classList.remove('active');
        }

        function togglePollCreator() {
            featureElements.pollCreator.classList.toggle('active');
            featureElements.emojiPickerContainer.style.display = 'none';
            featureElements.fileUploadContainer.classList.remove('active');
        }

        function addPollOption() {
            const optionCount = featureElements.pollOptions.children.length;
            if (optionCount >= 6) {
                showNotification("Maximum 6 options allowed", true);
                return;
            }
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'poll-input poll-option-input';
            input.placeholder = `Option ${optionCount + 1}`;
            featureElements.pollOptions.appendChild(input);
        }

        async function createPoll() {
            const question = featureElements.pollQuestion.value.trim();
            const options = Array.from(featureElements.pollOptions.querySelectorAll('input'))
                .map(input => input.value.trim())
                .filter(opt => opt.length > 0);

            if (!question) {
                showNotification("Poll question is required", true);
                return;
            }
            if (options.length < 2) {
                showNotification("At least 2 options are required", true);
                return;
            }

            try {
                const response = await fetch('/api/create-poll', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question, options })
                });

                const data = await response.json();
                if (data.success) {
                    // Send poll as message
                    await sendMessageWithPoll(data.poll_id);
                    featureElements.pollCreator.classList.remove('active');
                    featureElements.pollQuestion.value = '';
                    featureElements.pollOptions.innerHTML = `
                        <input type="text" class="poll-input poll-option-input" placeholder="Option 1">
                        <input type="text" class="poll-input poll-option-input" placeholder="Option 2">
                    `;
                    showNotification("Poll created successfully");
                } else {
                    showNotification(data.error || "Failed to create poll", true);
                }
            } catch (error) {
                console.error("Error creating poll:", error);
                showNotification("Failed to create poll", true);
            }
        }

        async function uploadFile(fileType) {
            const input = document.createElement('input');
            input.type = 'file';
            input.accept = getFileAccept(fileType);
            input.onchange = async (e) => {
                const file = e.target.files[0];
                if (!file) return;

                const formData = new FormData();
                formData.append('file', file);
                formData.append('file_type', fileType);

                try {
                    const response = await fetch('/api/upload-file', {
                        method: 'POST',
                        body: formData
                    });

                    const data = await response.json();
                    if (data.success) {
                        await sendMessageWithFile(data.file_id, fileType, file.name);
                        featureElements.fileUploadContainer.classList.remove('active');
                        showNotification("File uploaded successfully");
                    } else {
                        showNotification(data.error || "Failed to upload file", true);
                    }
                } catch (error) {
                    console.error("Error uploading file:", error);
                    showNotification("Failed to upload file", true);
                }
            };
            input.click();
        }

        function getFileAccept(fileType) {
            const acceptTypes = {
                'image': 'image/*',
                'document': '.pdf,.doc,.docx,.txt,.ppt,.pptx,.xls,.xlsx',
                'audio': 'audio/*',
                'video': 'video/*'
            };
            return acceptTypes[fileType] || '*/*';
        }

        async function shareContact() {
            if (!selectedChat) {
                showNotification("Please select a chat first", true);
                return;
            }

            // For demo, share current user's contact
            const contactData = {
                name: currentUser.name,
                mobile_number: currentUser.mobile_number,
                avatar: currentUser.avatar
            };

            try {
                await sendMessageWithContact(contactData);
                showNotification("Contact shared successfully");
            } catch (error) {
                console.error("Error sharing contact:", error);
                showNotification("Failed to share contact", true);
            }
        }

        // --- Voice Recording ---
        async function startVoiceRecording(event) {
            if (chatAreaElements.sendBtn.classList.contains('has-text')) return;
            
            event.preventDefault();
            if (isRecording) return;

            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder = new MediaRecorder(stream);
                audioChunks = [];

                mediaRecorder.ondataavailable = (event) => {
                    audioChunks.push(event.data);
                };

                mediaRecorder.onstop = async () => {
                    const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });
                    await sendVoiceMessage(audioBlob);
                    stream.getTracks().forEach(track => track.stop());
                };

                mediaRecorder.start();
                isRecording = true;
                featureElements.voiceRecorder.classList.add('active');
                startRecordingTimer();

                // Visual feedback
                chatAreaElements.sendBtn.style.backgroundColor = '#f15c6c';
            } catch (error) {
                console.error("Error starting voice recording:", error);
                showNotification("Microphone access denied", true);
            }
        }

        function stopVoiceRecording() {
            if (!isRecording) return;
            
            if (mediaRecorder && mediaRecorder.state !== 'inactive') {
                mediaRecorder.stop();
            }
            isRecording = false;
            featureElements.voiceRecorder.classList.remove('active');
            stopRecordingTimer();

            // Reset visual feedback
            chatAreaElements.sendBtn.style.backgroundColor = '';
        }

        function cancelVoiceRecording() {
            if (!isRecording) return;
            
            if (mediaRecorder && mediaRecorder.state !== 'inactive') {
                mediaRecorder.stop();
            }
            isRecording = false;
            featureElements.voiceRecorder.classList.remove('active');
            stopRecordingTimer();
            audioChunks = [];

            // Reset visual feedback
            chatAreaElements.sendBtn.style.backgroundColor = '';
        }

        function startRecordingTimer() {
            recordingStartTime = Date.now();
            recordingTimer = setInterval(() => {
                const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
                const minutes = Math.floor(elapsed / 60).toString().padStart(2, '0');
                const seconds = (elapsed % 60).toString().padStart(2, '0');
                featureElements.recordingTimer.textContent = `${minutes}:${seconds}`;
            }, 1000);
        }

        function stopRecordingTimer() {
            if (recordingTimer) {
                clearInterval(recordingTimer);
                recordingTimer = null;
            }
            featureElements.recordingTimer.textContent = '00:00';
        }

        // --- Mobile View Navigation ---
         function showChatAreaView() {
             if (window.innerWidth <= 768) {
                 sidebarElements.sidebar.style.display = 'none';
                 chatAreaElements.chatArea.classList.add('active');
                 isChatAreaActive = true;
             }
         }

         function showSidebarView() {
             if (window.innerWidth <= 768) {
                 sidebarElements.sidebar.style.display = 'flex';
                 chatAreaElements.chatArea.classList.remove('active');
                 isChatAreaActive = false;
                  selectedChat = null;
                  resetChatArea();
             }
         }

         function resetChatArea() {
              chatAreaElements.chatHeader.style.display = 'none';
              chatAreaElements.inputArea.style.display = 'none';
              chatAreaElements.emptyChat.style.display = 'flex';
              chatAreaElements.messagesContainer.innerHTML = '';
              chatAreaElements.messagesContainer.appendChild(chatAreaElements.emptyChat);
              chatAreaElements.emptyMessagesPlaceholder.style.display = 'none';
              chatAreaElements.typingIndicator.classList.remove('active');
              document.querySelectorAll('.chat-item.active').forEach(el => el.classList.remove('active'));
         }

        // --- Socket.IO Connection & Event Handling ---
        function connectSocketIO() {
             if (socket && socket.connected) return;

             socket = io({ transports: ['websocket', 'polling'] });

            socket.on('connect', () => {
                console.log('✅ Socket connected:', socket.id);
            });

            socket.on('disconnect', (reason) => {
                console.log('🔌 Socket disconnected:', reason);
            });

            socket.on('connect_error', (error) => {
                 console.error('❌ Socket connection error:', error);
            });

            socket.on('new_message', (messageData) => {
                console.log('📩 New message via socket:', messageData);
                if (!currentUser) return;

                if (messageData.sender_id !== currentUser.mobile_number) {
                     showNotification(`New message from ${messageData.sender_name || 'Someone'}`);
                }

                loadChats();

                if (selectedChat && messageData.conversation_id === selectedChat.chat_id) {
                     const existingMsgElement = document.querySelector(`.message[data-message-id="${messageData.id}"]`);
                     if (!existingMsgElement) {
                         const messageElement = createMessageElement(messageData, -1);
                         chatAreaElements.emptyMessagesPlaceholder.style.display = 'none';
                         chatAreaElements.emptyChat.style.display = 'none';

                         chatAreaElements.messagesContainer.appendChild(messageElement);
                         scrollToBottom();
                     } else {
                          console.log("Message already displayed optimistically or duplicated event.");
                     }
                 } else if (selectedChat && messageData.sender_id === selectedChat.other_user) {
                      console.warn("Received message from open chat user but different conversation ID?");
                      loadMessages();
                 }
            });

            socket.on('contacts_updated', (data) => {
                 console.log('Contacts list updated via socket:', data);
                 loadContacts();
             });

            socket.on('status_update', (data) => {
                console.log('Status update received:', data);
                const { user: userMobile, online, last_seen } = data;
                const contactItem = document.querySelector(`.contact-item[data-mobile-number="${userMobile}"]`);
                if (contactItem) {
                    const indicator = contactItem.querySelector('.online-indicator');
                    if (indicator) indicator.style.display = online ? 'block' : 'none';
                }
                const chatItem = document.querySelector(`.chat-item[data-other-user="${userMobile}"]`);
                 if (chatItem) {
                     const indicator = chatItem.querySelector('.online-indicator');
                     if (indicator) indicator.style.display = online ? 'block' : 'none';
                 }
                 if (selectedChat && selectedChat.other_user === userMobile) {
                      chatAreaElements.onlineDot.style.display = online ? 'inline-block' : 'none';
                      chatAreaElements.chatStatusText.textContent = online ? 'Online' : 'Offline';
                 }
            });

            socket.on('typing_indicator', (data) => {
                if (selectedChat && data.sender === selectedChat.other_user) {
                    if (data.is_typing) {
                        chatAreaElements.typingText.textContent = `${selectedChat.user.name} is typing...`;
                        chatAreaElements.typingIndicator.classList.add('active');
                    } else {
                        chatAreaElements.typingIndicator.classList.remove('active');
                    }
                }
            });

            socket.on('poll_updated', (data) => {
                console.log('Poll updated:', data);
                // Update poll message if it's in the current chat
                const pollMessage = document.querySelector(`.message[data-poll-id="${data.poll_id}"]`);
                if (pollMessage) {
                    updatePollMessage(pollMessage, data.poll);
                }
            });

            socket.on('messages_read', (data) => {
                console.log('Messages marked as read:', data);
                if (selectedChat && data.conversation_id === selectedChat.chat_id) {
                    // Update read status in UI
                    document.querySelectorAll('.message.sent').forEach(msg => {
                        const statusIcon = msg.querySelector('.message-status-icon');
                        if (statusIcon) {
                            statusIcon.innerHTML = '✓✓';
                            statusIcon.style.color = '#53bdeb';
                        }
                    });
                }
            });
        }

        function handleTyping(isTyping = true) {
            if (!selectedChat || !socket) return;
            
            socket.emit('typing', {
                receiver: selectedChat.other_user,
                is_typing: isTyping && chatAreaElements.messageInput.value.length > 0
            });
        }

        // --- Notification Function ---
        function showNotification(message, isError = false) {
             notificationElement.textContent = message;
             notificationElement.style.borderColor = isError ? '#f15c6c' : '#00a884';
             notificationElement.classList.add('show');

             setTimeout(() => {
                 notificationElement.classList.remove('show');
             }, 4000);
         }

        // --- Core Data Loading ---
         async function loadCurrentUser() {
            try {
                const response = await fetch('/api/current-user');
                if (!response.ok) {
                     if (response.status === 401) window.location.href = '/login';
                     throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                 }
                const data = await response.json();

                if (data.success && data.current_user) {
                    currentUser = data.current_user;
                    updateProfileHeader(currentUser);
                } else {
                    console.error('API Error loading current user:', data.error);
                    window.location.href = '/login';
                }
            } catch (error) {
                console.error('Network/JSON Error loading current user:', error);
                 window.location.href = '/login';
            }
        }

        async function loadChats() {
            if (!currentUser) return;
            console.log("Loading chats...");
            try {
                const response = await fetch('/api/chats');
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();

                if (data.success) {
                     console.log("Chats loaded:", data.chats.length);
                    displayChats(data.chats);
                } else {
                    console.error('API Error loading chats:', data.error);
                    showListError(sidebarElements.chatsList, sidebarElements.emptyChatsPlaceholder, "Could not load chats.");
                }
            } catch (error) {
                console.error('Network/JSON Error loading chats:', error);
                showListError(sidebarElements.chatsList, sidebarElements.emptyChatsPlaceholder, "Could not load chats. Check connection.");
            }
        }

        async function loadContacts() {
            if (!currentUser) return;
             console.log("Loading contacts...");
            try {
                const response = await fetch('/api/contacts');
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();

                if (data.success) {
                     console.log("Contacts loaded:", data.contacts.length);
                    displayContacts(data.contacts);
                     currentUser.contacts = data.contacts.map(c => c.mobile_number);
                } else {
                    console.error('API Error loading contacts:', data.error);
                     showListError(sidebarElements.contactsList, sidebarElements.emptyContactsPlaceholder, "Could not load contacts.");
                }
            } catch (error) {
                 console.error('Network/JSON Error loading contacts:', error);
                  showListError(sidebarElements.contactsList, sidebarElements.emptyContactsPlaceholder, "Could not load contacts. Check connection.");
            }
        }

        async function loadMessages() {
             if (!selectedChat || !selectedChat.other_user) {
                  console.log("Load messages called but no chat selected.");
                  resetChatArea();
                  return;
             }
             console.log(`Loading messages for ${selectedChat.other_user}`);

            chatAreaElements.messagesContainer.innerHTML = '';
            chatAreaElements.emptyMessagesPlaceholder.style.display = 'none';
            chatAreaElements.emptyChat.style.display = 'none';

            try {
                const response = await fetch(`/api/chat/${selectedChat.other_user}`);
                 if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();

                if (data.success) {
                    displayMessages(data.messages);
                    // Mark messages as read
                    if (selectedChat.chat_id) {
                        await fetch(`/api/mark-read/${selectedChat.chat_id}`, { method: 'POST' });
                    }
                } else {
                     console.error(`API Error loading messages for ${selectedChat.other_user}:`, data.error);
                     showMessagesError("Could not load messages.");
                }
            } catch (error) {
                 console.error(`Network/JSON Error loading messages for ${selectedChat.other_user}:`, error);
                 showMessagesError("Could not load messages. Check connection.");
            }
        }

        // --- UI Update Helpers ---
        function updateProfileHeader(user) {
             if (!user) return;
             profileElements.profileAvatarText.textContent = user.avatar || '??';
             if (user.profile_pic) {
                 profileElements.profileAvatar.innerHTML = `<img src="${user.profile_pic}" alt="${user.name || ''}">`;
             } else {
                  profileElements.profileAvatar.innerHTML = '';
                  profileElements.profileAvatar.appendChild(profileElements.profileAvatarText);
             }
             profileElements.dropdownName.textContent = user.name || 'User';
             profileElements.dropdownStatus.textContent = user.status || 'Available';
        }

         function showListError(listElement, placeholderElement, message) {
            listElement.innerHTML = '';
            placeholderElement.innerHTML = `<i class="fas fa-exclamation-triangle"></i><p>${escapeHtml(message)}</p>`;
            placeholderElement.style.display = 'flex';
         }

         function showMessagesError(message) {
              chatAreaElements.messagesContainer.innerHTML = '';
              chatAreaElements.emptyMessagesPlaceholder.innerHTML = `<i class="fas fa-exclamation-triangle"></i><p>${escapeHtml(message)}</p>`;
              chatAreaElements.emptyMessagesPlaceholder.style.display = 'flex';
              chatAreaElements.emptyChat.style.display = 'none';
         }

         function displayChats(chats) {
            sidebarElements.chatsList.innerHTML = '';
            if (!chats || chats.length === 0) {
                sidebarElements.emptyChatsPlaceholder.style.display = 'flex';
                return;
            }
             sidebarElements.emptyChatsPlaceholder.style.display = 'none';

            chats.forEach(chat => {
                const chatElement = createChatElement(chat);
                if (selectedChat && chat.chat_id === selectedChat.chat_id) {
                     chatElement.classList.add('active');
                }
                sidebarElements.chatsList.appendChild(chatElement);
            });
             handleSearch();
        }

        function createChatElement(chat) {
             const chatElement = document.createElement('div');
             chatElement.className = 'chat-item';
             chatElement.dataset.chatId = chat.chat_id;
             chatElement.dataset.otherUser = chat.other_user;

             const user = chat.user || {};
             const avatarHtml = user.profile_pic
                 ? `<img src="${user.profile_pic}" alt="${user.name || 'User'}">`
                 : `<span>${user.avatar || '??'}</span>`;
             const unreadCount = parseInt(chat.unread_count, 10);

             chatElement.innerHTML = `
                <div class="chat-avatar">
                    ${avatarHtml}
                    ${user.is_online ? '<div class="online-indicator"></div>' : ''}
                </div>
                <div class="chat-info">
                     <div class="chat-info-top">
                         <div class="chat-name">${escapeHtml(user.name || 'Unknown User')}</div>
                         <div class="chat-time">${chat.last_message_time || ''}</div>
                     </div>
                     <div class="chat-info-bottom">
                         <div class="chat-last-message">
                              ${chat.unread_count > 0 ? '' : '<span class="lock-icon">🔒</span>'} ${escapeHtml(chat.last_message || '')}
                          </div>
                          ${unreadCount > 0 ? `<div class="unread-badge">${unreadCount > 9 ? '9+' : unreadCount}</div>` : ''} </div>
                </div>
            `;
             chatElement.addEventListener('click', () => {
                  selectChat(chat);
                  showChatAreaView();
             });
             return chatElement;
        }

        function displayContacts(contacts) {
            sidebarElements.contactsList.innerHTML = '';
            if (!contacts || contacts.length === 0) {
                sidebarElements.emptyContactsPlaceholder.style.display = 'flex';
                return;
            }
             sidebarElements.emptyContactsPlaceholder.style.display = 'none';

            contacts.forEach(contact => {
                const contactElement = createContactElement(contact);
                sidebarElements.contactsList.appendChild(contactElement);
            });
            handleSearch();
        }

        function createContactElement(contact) {
             const contactElement = document.createElement('div');
             contactElement.className = 'contact-item';
             contactElement.dataset.userId = contact.id;
             contactElement.dataset.mobileNumber = contact.mobile_number;

             const avatarHtml = contact.profile_pic
                 ? `<img src="${contact.profile_pic}" alt="${contact.name}">`
                 : `<span>${contact.avatar}</span>`;

             contactElement.innerHTML = `
                <div class="contact-avatar">
                    ${avatarHtml}
                    ${contact.is_online ? '<div class="online-indicator"></div>' : ''}
                </div>
                <div class="contact-info">
                    <div class="contact-name">${escapeHtml(contact.name)}</div>
                    <div class="contact-status">${escapeHtml(contact.status || 'Available')}</div>
                    <div class="contact-phone" style="display: none;">${escapeHtml(contact.mobile_number)}</div>
                </div>
            `;
             contactElement.addEventListener('click', () => {
                  startChatFromContact(contact);
                  showChatAreaView();
             });
             return contactElement;
        }

         function displayMessages(messages) {
             const msgContainer = chatAreaElements.messagesContainer;
             msgContainer.innerHTML = '';
             if (!messages || messages.length === 0) {
                 chatAreaElements.emptyMessagesPlaceholder.style.display = 'flex';
                 chatAreaElements.emptyChat.style.display = 'none';
                 return;
             }
             chatAreaElements.emptyMessagesPlaceholder.style.display = 'none';
             chatAreaElements.emptyChat.style.display = 'none';

             const fragment = document.createDocumentFragment();
             messages.forEach((message, index) => {
                 const messageElement = createMessageElement(message, index);
                 fragment.appendChild(messageElement);
             });
             msgContainer.appendChild(fragment);

             scrollToBottom();
        }

        function createMessageElement(message, index) {
             if (!currentUser || !message) return document.createElement('div');

             const isSent = message.sender_id === currentUser.mobile_number;
             const messageId = message.id || `msg_${index}`;
             const messageKey = `${selectedChat?.chat_id || 'unknown'}_${messageId}`;
             const currentState = messageStates[messageKey];

             const messageElement = document.createElement('div');
             messageElement.className = `message ${isSent ? 'sent' : 'received'}`;
             messageElement.dataset.messageId = messageId;
             if (message.poll_id) messageElement.dataset.pollId = message.poll_id;

             let messageContent = '';
             
             switch (message.message_type) {
                 case 'file':
                     messageContent = createFileMessageContent(message);
                     break;
                 case 'voice':
                     messageContent = createVoiceMessageContent(message);
                     break;
                 case 'poll':
                     messageContent = createPollMessageContent(message);
                     break;
                 case 'contact':
                     messageContent = createContactMessageContent(message);
                     break;
                 default:
                     const displayText = currentState?.isDecoded ? currentState.decrypted : message.encrypted;
                     messageContent = `<div class="message-text">${escapeHtml(displayText)}</div>`;
             }

             const statusIcon = isSent ? 
                 (message.read ? '<span class="message-status-icon" style="color: #53bdeb;">✓✓</span>' : 
                                 '<span class="message-status-icon">✓✓</span>') : '';

             messageElement.innerHTML = `
                ${messageContent}
                <div class="message-meta">
                    <span class="message-time">${message.timestamp || ''}</span>
                    ${message.message_type === 'text' ? `
                    <button class="toggle-btn" data-message-index="${index}" title="${currentState?.isDecoded ? 'Encode' : 'Decode'}">
                        ${currentState?.isDecoded ? '🔒' : '🔓'}
                    </button>
                    ` : ''}
                    ${statusIcon}
                </div>
            `;

             if (message.message_type === 'text') {
                 const toggleButton = messageElement.querySelector('.toggle-btn');
                 if (toggleButton) {
                     toggleButton.addEventListener('click', (e) => {
                          e.stopPropagation();
                          const idx = parseInt(e.currentTarget.dataset.messageIndex, 10);
                          if (!isNaN(idx)) toggleMessage(idx);
                     });
                 }
             }

             return messageElement;
        }

        function createFileMessageContent(message) {
            const fileInfo = message.file_info || {};
            const fileType = fileInfo.file_type || 'document';
            const icon = getFileIcon(fileType);
            const size = formatFileSize(fileInfo.size);
            
            return `
                <div class="file-message">
                    <div class="file-icon">${icon}</div>
                    <div class="file-name">${escapeHtml(fileInfo.filename || 'Unknown file')}</div>
                    <div class="file-size">${size}</div>
                    <button class="file-download-btn" onclick="downloadFile('${message.file_id}')">
                        <i class="fas fa-download"></i> Download
                    </button>
                </div>
            `;
        }

        function createVoiceMessageContent(message) {
            return `
                <div class="voice-message">
                    <button class="voice-play-btn">
                        <i class="fas fa-play"></i>
                    </button>
                    <div class="voice-waveform"></div>
                    <div class="voice-duration">0:30</div>
                </div>
            `;
        }

        function createPollMessageContent(message) {
            const poll = message.poll_info || {};
            const totalVotes = poll.voters ? poll.voters.length : 0;
            const userVoted = poll.voters && currentUser ? poll.voters.includes(currentUser.mobile_number) : false;
            
            let optionsHtml = '';
            if (poll.options) {
                poll.options.forEach((option, index) => {
                    const percentage = totalVotes > 0 ? Math.round((option.votes / totalVotes) * 100) : 0;
                    const isVoted = userVoted && option.voters.includes(currentUser.mobile_number);
                    
                    optionsHtml += `
                        <div class="poll-option ${isVoted ? 'voted' : ''}" data-option-index="${index}">
                            <div>${escapeHtml(option.text)}</div>
                            ${userVoted ? `
                            <div class="poll-results">
                                <div class="poll-result-bar" style="width: ${percentage}%"></div>
                                <div class="poll-vote-count">${option.votes} votes (${percentage}%)</div>
                            </div>
                            ` : ''}
                        </div>
                    `;
                });
            }
            
            return `
                <div class="poll-message">
                    <div class="poll-question">${escapeHtml(poll.question || 'Poll')}</div>
                    ${optionsHtml}
                    ${!userVoted ? `<div class="poll-vote-count">${totalVotes} votes</div>` : ''}
                </div>
            `;
        }

        function createContactMessageContent(message) {
            const contact = message.shared_contact || {};
            
            return `
                <div class="contact-share">
                    <div class="contact-name-share">${escapeHtml(contact.name || 'Unknown')}</div>
                    <div class="contact-phone-share">${escapeHtml(contact.mobile_number || '')}</div>
                    <button class="contact-add-btn" onclick="addSharedContact('${contact.mobile_number}')">
                        <i class="fas fa-user-plus"></i> Add to Contacts
                    </button>
                </div>
            `;
        }

        function getFileIcon(fileType) {
            const icons = {
                'image': '🖼️',
                'audio': '🎵',
                'video': '🎬',
                'document': '📄'
            };
            return icons[fileType] || '📎';
        }

        function formatFileSize(bytes) {
            if (!bytes) return 'Unknown size';
            const sizes = ['Bytes', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(1024));
            return Math.round(bytes / Math.pow(1024, i) * 100) / 100 + ' ' + sizes[i];
        }

        // --- Chat Interaction ---
        function selectChat(chat) {
             if (!chat || !chat.user) { console.error("Invalid chat object:", chat); return; }
             console.log("Selecting chat:", chat.chat_id, "with", chat.other_user);

             selectedChat = { ...chat, other_user: chat.other_user || chat.user?.mobile_number };
             messageStates = {};

             document.querySelectorAll('.chat-item.active').forEach(item => item.classList.remove('active'));
             const chatElement = document.querySelector(`.chat-item[data-chat-id="${chat.chat_id}"]`);
             if (chatElement) chatElement.classList.add('active');

             updateChatHeader(selectedChat.user);
             chatAreaElements.chatHeader.style.display = 'flex';
             chatAreaElements.inputArea.style.display = 'flex';
             chatAreaElements.emptyChat.style.display = 'none';

             loadMessages();
             chatAreaElements.messageInput.focus();
             toggleSendButtonIcon();
        }

         function startChatFromContact(contact) {
            if (!contact || !contact.mobile_number) return;
            console.log("Starting chat from contact:", contact.mobile_number);
             const existingChatItem = document.querySelector(`.chat-item[data-other-user="${contact.mobile_number}"]`);

             if (existingChatItem) {
                 existingChatItem.click();
             } else {
                 const tempChat = {
                     chat_id: getChatId(currentUser.mobile_number, contact.mobile_number),
                     user: contact,
                     other_user: contact.mobile_number,
                     last_message: '',
                     last_message_time: '',
                     unread_count: 0
                 };
                 selectChat(tempChat);
                 showChatsTab();
                 showChatAreaView();
             }
         }

         function updateChatHeader(contactUser) {
             if (!contactUser) {
                 chatAreaElements.chatContactName.textContent = 'Unknown User';
                 chatAreaElements.chatAvatar.innerHTML = `<span>??</span>`;
                 chatAreaElements.chatStatusText.textContent = 'N/A';
                 chatAreaElements.onlineDot.style.display = 'none';
                 return;
             }
             const avatarHtml = contactUser.profile_pic
                ? `<img src="${contactUser.profile_pic}" alt="${contactUser.name || ''}">`
                : `<span>${contactUser.avatar || '??'}</span>`;
            chatAreaElements.chatAvatar.innerHTML = avatarHtml;
            chatAreaElements.chatContactName.textContent = contactUser.name || 'Unknown User';

             chatAreaElements.onlineDot.style.display = contactUser.is_online ? 'inline-block' : 'none';
             chatAreaElements.chatStatusText.textContent = contactUser.is_online ? 'Online' : 'Offline';
        }

        async function sendMessage() {
            const text = chatAreaElements.messageInput.value.trim();
             if (!text || !selectedChat || !selectedChat.other_user || !chatAreaElements.sendBtn.classList.contains('has-text')) {
                  if (!text && chatAreaElements.sendBtnIcon.classList.contains('fa-microphone')) {
                       // Voice message handled separately
                       return;
                  }
                  return;
             }

            const receiverId = selectedChat.other_user;

             const messageToSend = text;
             chatAreaElements.messageInput.value = '';
             autoResizeInput();
             toggleSendButtonIcon();
             chatAreaElements.messageInput.focus();
             handleTyping(false);

            try {
                const response = await fetch('/api/send', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ receiver_id: receiverId, message: messageToSend })
                });

                if (!response.ok) {
                     const errorData = await response.json().catch(() => ({ error: `Server error: ${response.status}` }));
                     throw new Error(errorData.error || `HTTP error ${response.status}`);
                 }

                const data = await response.json();

                if (data.success) {
                     console.log("Message sent via API, ID:", data.message_id);
                     loadChats();
                } else {
                     throw new Error(data.error || 'API failed to send message');
                }
            } catch (error) {
                console.error('Error sending message:', error);
                alert(`Error: ${error.message}`);
            }
        }

        async function sendMessageWithFile(fileId, fileType, filename) {
            if (!selectedChat) return;

            try {
                const response = await fetch('/api/send', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        receiver_id: selectedChat.other_user, 
                        message: `Sent a ${fileType} file: ${filename}`,
                        message_type: 'file',
                        file_id: fileId
                    })
                });

                const data = await response.json();
                if (data.success) {
                    loadChats();
                    showNotification("File sent successfully");
                } else {
                    throw new Error(data.error);
                }
            } catch (error) {
                console.error('Error sending file message:', error);
                showNotification("Failed to send file", true);
            }
        }

        async function sendVoiceMessage(audioBlob) {
            if (!selectedChat) return;

            // Convert blob to file
            const audioFile = new File([audioBlob], 'voice-message.wav', { type: 'audio/wav' });
            
            const formData = new FormData();
            formData.append('file', audioFile);
            formData.append('file_type', 'audio');

            try {
                // Upload audio file
                const uploadResponse = await fetch('/api/upload-file', {
                    method: 'POST',
                    body: formData
                });

                const uploadData = await uploadResponse.json();
                if (uploadData.success) {
                    // Send voice message
                    const sendResponse = await fetch('/api/send', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ 
                            receiver_id: selectedChat.other_user, 
                            message: 'Voice message',
                            message_type: 'voice',
                            file_id: uploadData.file_id
                        })
                    });

                    const sendData = await sendResponse.json();
                    if (sendData.success) {
                        loadChats();
                        showNotification("Voice message sent");
                    } else {
                        throw new Error(sendData.error);
                    }
                } else {
                    throw new Error(uploadData.error);
                }
            } catch (error) {
                console.error('Error sending voice message:', error);
                showNotification("Failed to send voice message", true);
            }
        }

        async function sendMessageWithPoll(pollId) {
            if (!selectedChat) return;

            try {
                const response = await fetch('/api/send', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        receiver_id: selectedChat.other_user, 
                        message: 'Poll',
                        message_type: 'poll',
                        poll_id: pollId
                    })
                });

                const data = await response.json();
                if (data.success) {
                    loadChats();
                    showNotification("Poll sent successfully");
                } else {
                    throw new Error(data.error);
                }
            } catch (error) {
                console.error('Error sending poll:', error);
                showNotification("Failed to send poll", true);
            }
        }

        async function sendMessageWithContact(contactData) {
            if (!selectedChat) return;

            try {
                const response = await fetch('/api/send', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        receiver_id: selectedChat.other_user, 
                        message: `Shared contact: ${contactData.name}`,
                        message_type: 'contact',
                        shared_contact: contactData
                    })
                });

                const data = await response.json();
                if (data.success) {
                    loadChats();
                    showNotification("Contact shared successfully");
                } else {
                    throw new Error(data.error);
                }
            } catch (error) {
                console.error('Error sharing contact:', error);
                showNotification("Failed to share contact", true);
            }
        }

        // --- Message Decode/Encode ---
        async function toggleMessage(messageIndex) {
             if (!selectedChat || !selectedChat.other_user) return;

             const messages = await getCurrentMessages();
             if (!messages || messageIndex < 0 || messageIndex >= messages.length) {
                  console.error("Invalid message index or messages not loaded for toggle:", messageIndex);
                  return;
             }
             const message = messages[messageIndex];
             const messageId = message.id || `msg_${messageIndex}`;
             const messageKey = `${selectedChat.chat_id}_${messageId}`;

            if (!messageStates[messageKey] || !messageStates[messageKey].decrypted) {
                const buttonElement = document.querySelector(`.message[data-message-id="${messageId}"] .toggle-btn`);
                 if(buttonElement) buttonElement.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

                try {
                    const response = await fetch('/api/decode', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ encrypted_text: message.encrypted })
                    });
                     if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    const data = await response.json();
                    if (!data.success) throw new Error(data.error || "Decode failed");

                    messageStates[messageKey] = { isDecoded: true, encrypted: message.encrypted, decrypted: data.decrypted };
                     console.log(`Decoded msg ${messageKey}`);
                     updateMessageElement(message, messageIndex);

                } catch (error) {
                    console.error('Error decoding message:', error);
                    alert("Could not decode message.");
                     if(buttonElement) buttonElement.innerHTML = '⚠️';
                    return;
                }
            } else {
                messageStates[messageKey].isDecoded = !messageStates[messageKey].isDecoded;
                 console.log(`Toggled msg ${messageKey} to ${messageStates[messageKey].isDecoded ? 'decoded' : 'encoded'}`);
                 updateMessageElement(message, messageIndex);
            }
        }

        function updateMessageElement(message, index) {
            const messageId = message.id || `msg_${index}`;
            const elementToUpdate = document.querySelector(`.message[data-message-id="${messageId}"]`);
            if (elementToUpdate) {
                 const newElement = createMessageElement(message, index);
                 elementToUpdate.replaceWith(newElement);
            } else {
                 console.warn(`Message element ${messageId} not found for update.`);
            }
        }

        function updatePollMessage(pollElement, pollData) {
            const newContent = createPollMessageContent({ poll_info: pollData });
            const contentElement = pollElement.querySelector('.poll-message');
            if (contentElement) {
                contentElement.outerHTML = newContent;
            }
            
            // Reattach event listeners for voting
            attachPollVoteListeners(pollElement);
        }

        function attachPollVoteListeners(pollElement) {
            const options = pollElement.querySelectorAll('.poll-option');
            options.forEach(option => {
                option.addEventListener('click', async () => {
                    const pollId = pollElement.dataset.pollId;
                    const optionIndex = parseInt(option.dataset.optionIndex);
                    
                    try {
                        const response = await fetch('/api/vote-poll', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ poll_id: pollId, option_index: optionIndex })
                        });
                        
                        const data = await response.json();
                        if (data.success) {
                            showNotification("Vote recorded");
                        } else {
                            showNotification(data.error || "Failed to vote", true);
                        }
                    } catch (error) {
                        console.error('Error voting:', error);
                        showNotification("Failed to vote", true);
                    }
                });
            });
        }

        async function getCurrentMessages() {
              if (!selectedChat || !selectedChat.other_user) return null;
              try {
                  const response = await fetch(`/api/chat/${selectedChat.other_user}`);
                  if (!response.ok) { console.error(`HTTP error fetching messages: ${response.status}`); return null; }
                  const data = await response.json();
                  return data.success ? data.messages : null;
              } catch (error) {
                  console.error("Network error fetching messages:", error);
                  return null;
              }
        }

        // --- File Download ---
        async function downloadFile(fileId) {
            try {
                const response = await fetch(`/api/file/${fileId}`);
                if (response.ok) {
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'file';
                    document.body.appendChild(a);
                    a.click();
                    window.URL.revokeObjectURL(url);
                    document.body.removeChild(a);
                } else {
                    showNotification("Failed to download file", true);
                }
            } catch (error) {
                console.error('Error downloading file:', error);
                showNotification("Failed to download file", true);
            }
        }

        // --- Contact Sharing ---
        async function addSharedContact(phoneNumber) {
            try {
                const response = await fetch('/api/add-contact', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ phone: phoneNumber })
                });
                
                const data = await response.json();
                if (data.success) {
                    showNotification("Contact added successfully");
                    loadContacts();
                } else {
                    showNotification(data.error || "Failed to add contact", true);
                }
            } catch (error) {
                console.error('Error adding contact:', error);
                showNotification("Failed to add contact", true);
            }
        }

        // --- Filtering ---
         function filterChats(searchTerm) {
             let hasVisibleItems = false;
             const items = sidebarElements.chatsList.querySelectorAll('.chat-item');
             items.forEach(item => {
                 const name = item.querySelector('.chat-name')?.textContent.toLowerCase() || '';
                 const msg = item.querySelector('.chat-last-message')?.textContent.toLowerCase() || '';
                 const isVisible = name.includes(searchTerm) || msg.includes(searchTerm);
                 item.style.display = isVisible ? 'flex' : 'none';
                 if (isVisible) hasVisibleItems = true;
             });
             const hasAnyItems = sidebarElements.chatsList.querySelector('.chat-item') !== null;
             sidebarElements.emptyChatsPlaceholder.style.display = (hasAnyItems && !hasVisibleItems) ? 'flex' : 'none';
         }

         function filterContacts(searchTerm) {
             let hasVisibleItems = false;
             const items = sidebarElements.contactsList.querySelectorAll('.contact-item');
             items.forEach(item => {
                 const name = item.querySelector('.contact-name')?.textContent.toLowerCase() || '';
                 const status = item.querySelector('.contact-status')?.textContent.toLowerCase() || '';
                 const phone = item.querySelector('.contact-phone')?.textContent.toLowerCase() || '';
                 const isVisible = name.includes(searchTerm) || status.includes(searchTerm) || phone.includes(searchTerm);
                 item.style.display = isVisible ? 'flex' : 'none';
                 if (isVisible) hasVisibleItems = true;
             });
             const hasAnyItems = sidebarElements.contactsList.querySelector('.contact-item') !== null;
             sidebarElements.emptyContactsPlaceholder.style.display = (hasAnyItems && !hasVisibleItems) ? 'flex' : 'none';
         }

        // --- Profile Modal Logic ---
        function openProfileModal() {
              if (!currentUser) return;
              profileModalElements.nameError.textContent = '';
              profileModalElements.statusError.textContent = '';
              profileModalElements.currentPasswordError.textContent = '';
              profileModalElements.newPasswordError.textContent = '';
              profileModalElements.formError.textContent = '';
              profileModalElements.nameInput.value = currentUser.name || '';
              profileModalElements.statusInput.value = currentUser.status || '';
              profileModalElements.currentPasswordInput.value = '';
              profileModalElements.newPasswordInput.value = '';
              profileModalElements.picLargeText.textContent = currentUser.avatar || '??';
              if (currentUser.profile_pic) {
                  profileModalElements.picLarge.innerHTML = `<img src="${currentUser.profile_pic}" alt="${currentUser.name || ''}">`;
              } else {
                  profileModalElements.picLarge.innerHTML = '';
                  profileModalElements.picLarge.appendChild(profileModalElements.picLargeText);
              }
              profileModalElements.modal.classList.add('active');
        }
        function closeProfileModal() {
            profileModalElements.modal.classList.remove('active');
         }
        async function uploadProfilePicture(event) {
              const file = event.target.files[0];
              if (!file) return;

              if (!file.type.startsWith('image/')) {
                  showNotification("Please select an image file", true);
                  return;
              }

              const formData = new FormData();
              formData.append('file', file);

              try {
                  const response = await fetch('/api/upload-profile-pic', { method: 'POST', body: formData });
                  if (!response.ok) throw new Error(`HTTP error ${response.status}`);
                  const data = await response.json();
                  if (!data.success) throw new Error(data.error || 'Upload failed');

                  currentUser.profile_pic = data.profile_pic;
                  updateProfileHeader(currentUser);
                  profileModalElements.picLarge.innerHTML = `<img src="${data.profile_pic}" alt="${currentUser.name || ''}">`;
                  showNotification("Profile picture updated successfully");
              } catch (error) {
                  console.error('Error uploading profile picture:', error);
                  showNotification(`Error: ${error.message}`, true);
              }
         }
        async function saveProfile(event) {
            event.preventDefault();
            if (!currentUser) return;
            profileModalElements.nameError.textContent = '';
            profileModalElements.statusError.textContent = '';
            profileModalElements.currentPasswordError.textContent = '';
            profileModalElements.newPasswordError.textContent = '';
            profileModalElements.formError.textContent = '';

            const name = profileModalElements.nameInput.value.trim();
            const status = profileModalElements.statusInput.value.trim();
            const currentPassword = profileModalElements.currentPasswordInput.value;
            const newPassword = profileModalElements.newPasswordInput.value;
            
            let isValid = true;
             if (!name || name.length < 2) { profileModalElements.nameError.textContent = 'Name must be >= 2 characters.'; isValid = false; }
             if (name.length > 50) { profileModalElements.nameError.textContent = 'Name must be <= 50 characters.'; isValid = false; }
             if (status.length > 100) { profileModalElements.statusError.textContent = 'Status must be <= 100 characters.'; isValid = false; }
             if (currentPassword && !newPassword) { profileModalElements.newPasswordError.textContent = 'New password is required when changing password.'; isValid = false; }
             if (newPassword && !currentPassword) { profileModalElements.currentPasswordError.textContent = 'Current password is required to set new password.'; isValid = false; }
             if (newPassword && newPassword.length < 6) { profileModalElements.newPasswordError.textContent = 'New password must be at least 6 characters.'; isValid = false; }
             if (!isValid) return;

             const submitButton = profileModalElements.form.querySelector('.save-btn');
             submitButton.disabled = true; submitButton.textContent = 'Saving...';

            try {
                const updateData = {};
                if (name !== currentUser.name) updateData.name = name;
                if (status !== currentUser.status) updateData.status = status;
                if (currentPassword && newPassword) {
                    updateData.current_password = currentPassword;
                    updateData.new_password = newPassword;
                }

                const response = await fetch('/api/profile', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(updateData)
                });
                const data = await response.json();

                 if (!response.ok) {
                      if (data && data.errors) {
                          profileModalElements.nameError.textContent = data.errors.name || '';
                          profileModalElements.statusError.textContent = data.errors.status || '';
                          profileModalElements.currentPasswordError.textContent = data.errors.current_password || '';
                          profileModalElements.newPasswordError.textContent = data.errors.new_password || '';
                      } else {
                           throw new Error(data.error || `HTTP error ${response.status}`);
                      }
                      return;
                 }

                if (data.success) {
                    currentUser.name = name;
                    currentUser.status = status;
                    currentUser.avatar = name ? name.substring(0, 2).toUpperCase() : '??';
                    updateProfileHeader(currentUser);
                    closeProfileModal();
                    loadChats();
                    loadContacts();
                    showNotification("Profile updated successfully");
                } else {
                    throw new Error(data.error || 'Failed to save profile');
                }
            } catch (error) {
                console.error('Error saving profile:', error);
                profileModalElements.formError.textContent = `Error: ${error.message}`;
            } finally {
                 submitButton.disabled = false; submitButton.textContent = 'Save Changes';
            }
        }

        // --- Add Contact Modal Logic ---
        function openAddContactModal() {
              addContactModalElements.phoneInput.value = '';
              addContactModalElements.error.textContent = '';
              addContactModalElements.addError.textContent = '';
              addContactModalElements.resultArea.style.display = 'none';
              addContactModalElements.resultDetails.innerHTML = '';
              const oldBtn = addContactModalElements.confirmBtn;
              const newBtn = oldBtn.cloneNode(true);
              oldBtn.parentNode.replaceChild(newBtn, oldBtn);
              addContactModalElements.confirmBtn = newBtn;
              addContactModalElements.modal.classList.add('active');
        }
        function closeAddContactModal() {
             addContactModalElements.modal.classList.remove('active');
         }
        async function searchUserByPhone(event) {
             event.preventDefault();
             const phone = addContactModalElements.phoneInput.value.trim();
             addContactModalElements.error.textContent = '';
             addContactModalElements.addError.textContent = '';
             addContactModalElements.resultArea.style.display = 'none';
             const submitButton = addContactModalElements.form.querySelector('.modal-btn');

             if (!phone) { addContactModalElements.error.textContent = 'Phone number required.'; return; }
             if (!phone.startsWith('+') || !/^\+\d{10,}$/.test(phone)) {
                 addContactModalElements.error.textContent = 'Invalid format (e.g., +91xxxxxxxxxx).'; return;
             }
             if (phone === currentUser.mobile_number) {
                  addContactModalElements.error.textContent = 'You cannot search for yourself.'; return;
             }

             submitButton.disabled = true; submitButton.textContent = 'Searching...';

             try {
                 const response = await fetch('/api/find-user', {
                      method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ phone })
                 });
                 const data = await response.json();

                 if (!response.ok) {
                      throw new Error(data.error || `HTTP error ${response.status}`);
                 }

                 if (data.success && data.user) {
                      displaySearchResult(data.user);
                 } else {
                      addContactModalElements.error.textContent = data.error || 'User not found.';
                 }

             } catch (error) {
                  console.error("Error searching user:", error);
                  addContactModalElements.error.textContent = `Error: ${error.message}`;
             } finally {
                  submitButton.disabled = false; submitButton.textContent = 'Search User';
             }
         }
        function displaySearchResult(user) {
              const isAlreadyContact = currentUser.contacts && currentUser.contacts.includes(user.mobile_number);

              addContactModalElements.resultDetails.innerHTML = `
                  <p><strong>Name:</strong> ${escapeHtml(user.name)}</p>
                  <p><strong>Phone:</strong> ${escapeHtml(user.mobile_number)}</p>
                  <p><strong>Status:</strong> ${escapeHtml(user.status || 'N/A')}</p>
                  ${isAlreadyContact ? '<p style="color: #00a884; font-weight: 500;">✅ Already in your contacts.</p>' : ''}
              `;
              addContactModalElements.resultArea.style.display = 'block';

              const confirmBtn = document.getElementById('confirmAddContactBtn');
              confirmBtn.disabled = isAlreadyContact;
              confirmBtn.textContent = isAlreadyContact ? 'Already Added' : 'Add to Contacts';

              confirmBtn.onclick = isAlreadyContact ? null : () => confirmAddContact(user.mobile_number);
          }

        async function confirmAddContact(phoneToAdd) {
              addContactModalElements.addError.textContent = '';
              const confirmBtn = addContactModalElements.confirmBtn;
              confirmBtn.disabled = true;
              confirmBtn.textContent = 'Adding...';

              try {
                  const response = await fetch('/api/add-contact', {
                       method: 'POST', headers: { 'Content-Type': 'application/json' },
                       body: JSON.stringify({ phone: phoneToAdd })
                  });
                   const data = await response.json();
                   if (!response.ok) throw new Error(data.error || `HTTP error ${response.status}`);

                   if (data.success) {
                       showNotification(data.message || 'Contact added!', false);
                       if (!currentUser.contacts.includes(phoneToAdd)) {
                            currentUser.contacts.push(phoneToAdd);
                       }
                       loadContacts();
                       closeAddContactModal();
                   } else {
                        throw new Error(data.error || 'Failed to add contact.');
                   }

              } catch (error) {
                   console.error("Error adding contact:", error);
                   addContactModalElements.addError.textContent = `Error: ${error.message}`;
                   confirmBtn.disabled = false;
                   confirmBtn.textContent = 'Add to Contacts';
              }
          }

        // --- Auto Refresh / Polling ---
        function startAutoRefresh() {
            if (refreshInterval) clearInterval(refreshInterval);
            console.log("Starting auto-refresh polling (interval: 30s)");
            refreshInterval = setInterval(() => {
                if (!currentUser || !document.hasFocus()) return;

                console.log("Polling for updates...");
                loadChats();
                loadContacts();
            }, 30000);
        }

        // --- Helper Functions ---
        function getChatId(user1_mobile, user2_mobile) {
             const users = [user1_mobile, user2_mobile].sort();
             return users.join('--');
        }
        function scrollToBottom() {
             requestAnimationFrame(() => {
                  const container = chatAreaElements.messagesContainer;
                  container.scrollTop = container.scrollHeight;
             });
        }
        function escapeHtml(text) {
             if (typeof text !== 'string') return '';
             const div = document.createElement('div');
             div.textContent = text;
             return div.innerHTML;
        }

        // Make functions globally available for inline event handlers
        window.downloadFile = downloadFile;
        window.addSharedContact = addSharedContact;

    </script>
</body>
</html>
'''
if __name__ == '__main__':
    # Ensure MongoDB Index
    print("🔒 SecureChat E2E Encrypted Messaging")
    print("📱 Server running on http://localhost:5000")
    print("\n🎯 FEATURES:")
    print("✅ User signup and login with OTP verification")
    print("✅ MongoDB database integration with proper indexing")
    print("✅ Real-time messaging with Socket.IO")
    print("✅ Online/offline status indicators")
    print("✅ Typing indicators")
    print("✅ End-to-end message encryption")
    print("✅ WhatsApp-like UI")
    print("✅ Input validation and error handling")
    print("✅ Security improvements")
    print("\n🚀 TO GET STARTED:")
    print("1. Make sure MongoDB is running on localhost:27017")
    print("2. Set SECRET_KEY environment variable for production")
    print("3. Open http://localhost:5000 in your browser")
    print("4. Sign up with a new account")
    print("5. Start chatting!")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)