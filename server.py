import os
import json
import time
import math
import sqlite3
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'bff-secret-key-2026')
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

DB_FILE = os.environ.get('DB_PATH', 'bff_server.db')

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT,
            avatar_url TEXT,
            google_id TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            battery_percent INTEGER DEFAULT 100,
            is_charging BOOLEAN DEFAULT 0,
            activity TEXT DEFAULT 'Active',
            speed_kmh INTEGER DEFAULT 0,
            music_title TEXT DEFAULT '',
            music_artist TEXT DEFAULT '',
            is_online BOOLEAN DEFAULT 1,
            last_updated INTEGER NOT NULL
        )
    ''')
    
    # Location history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS location_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            speed_kmh INTEGER DEFAULT 0,
            timestamp INTEGER NOT NULL,
            label TEXT
        )
    ''')
    
    # Safe zones table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS safe_zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            radius_meters REAL NOT NULL,
            icon TEXT DEFAULT '🛡️',
            color_hex TEXT DEFAULT '#10B981',
            notify_entry BOOLEAN DEFAULT 1,
            notify_exit BOOLEAN DEFAULT 1
        )
    ''')
    
    # Chat messages table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            text TEXT,
            message_type TEXT DEFAULT 'text',
            voice_data TEXT,
            voice_duration_ms INTEGER DEFAULT 0,
            timestamp INTEGER NOT NULL,
            is_read BOOLEAN DEFAULT 0
        )
    ''')
    
    # Music history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS music_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            album_emoji TEXT DEFAULT '🎵',
            played_at INTEGER NOT NULL
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

user_zone_states = {}

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# ----------------- SOCKET.IO EVENTS -----------------

@socketio.on('connect')
def handle_connect():
    emit('server_status', {
        'status': 'connected',
        'server_time': int(time.time() * 1000),
        'message': 'Connected to BFF Real-Time Location Server'
    })

@socketio.on('disconnect')
def handle_disconnect():
    pass

@socketio.on('join')
def handle_join(data):
    user_id = data.get('user_id')
    if user_id:
        join_room(user_id)
        join_room('global_map_stream')
        emit('joined_ack', {'user_id': user_id, 'status': 'ok'})

@socketio.on('update_location')
def handle_update_location(data):
    user_id = data.get('user_id')
    if not user_id:
        return
    
    lat = float(data.get('latitude', 0.0))
    lng = float(data.get('longitude', 0.0))
    battery = int(data.get('battery_percent', 100))
    is_charging = bool(data.get('is_charging', False))
    activity = data.get('current_activity', 'Active')
    speed = int(data.get('speed_kmh', 0))
    music_title = data.get('music_title', '')
    music_artist = data.get('music_artist', '')
    email = data.get('email', '')
    avatar_url = data.get('avatar_url', '')
    google_id = data.get('google_id', '')
    name = data.get('name', user_id)
    now = int(time.time() * 1000)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (id, name, email, avatar_url, google_id, latitude, longitude, battery_percent, is_charging, 
                           activity, speed_kmh, music_title, music_artist, is_online, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            email = COALESCE(NULLIF(excluded.email, ''), users.email),
            avatar_url = COALESCE(NULLIF(excluded.avatar_url, ''), users.avatar_url),
            google_id = COALESCE(NULLIF(excluded.google_id, ''), users.google_id),
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            battery_percent = excluded.battery_percent,
            is_charging = excluded.is_charging,
            activity = excluded.activity,
            speed_kmh = excluded.speed_kmh,
            music_title = excluded.music_title,
            music_artist = excluded.music_artist,
            is_online = 1,
            last_updated = excluded.last_updated
    ''', (user_id, name, email, avatar_url, google_id, lat, lng, battery, is_charging, activity, speed, music_title, music_artist, now))

    cursor.execute('''
        INSERT INTO location_history (user_id, latitude, longitude, speed_kmh, timestamp, label)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, lat, lng, speed, now, activity))

    # Geofence check
    zones = cursor.execute('SELECT * FROM safe_zones').fetchall()
    for z in zones:
        dist = haversine_distance(lat, lng, z['latitude'], z['longitude'])
        is_inside = dist <= z['radius_meters']
        key = (user_id, z['id'])
        prev_inside = user_zone_states.get(key, None)
        
        if prev_inside is not None:
            if not prev_inside and is_inside and z['notify_entry']:
                socketio.emit('geofence_trigger', {
                    'user_id': user_id,
                    'user_name': name,
                    'zone_name': z['name'],
                    'event': 'ENTRY',
                    'icon': z['icon'],
                    'timestamp': now
                }, room='global_map_stream')
            elif prev_inside and not is_inside and z['notify_exit']:
                socketio.emit('geofence_trigger', {
                    'user_id': user_id,
                    'user_name': name,
                    'zone_name': z['name'],
                    'event': 'EXIT',
                    'icon': z['icon'],
                    'timestamp': now
                }, room='global_map_stream')
        user_zone_states[key] = is_inside

    conn.commit()
    conn.close()

    socketio.emit('friend_location_update', {
        'user_id': user_id,
        'name': name,
        'email': email,
        'avatar_url': avatar_url,
        'latitude': lat,
        'longitude': lng,
        'battery_percent': battery,
        'is_charging': is_charging,
        'current_activity': activity,
        'speed_kmh': speed,
        'music_title': music_title,
        'music_artist': music_artist,
        'timestamp': now
    }, room='global_map_stream', include_self=False)

@socketio.on('send_message')
def handle_send_message(data):
    sender_id = data.get('sender_id')
    recipient_id = data.get('recipient_id')
    text = data.get('text', '')
    m_type = data.get('type', 'text')
    voice_data = data.get('voice_data', '')
    voice_duration = data.get('voice_duration_ms', 0)
    now = int(time.time() * 1000)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO messages (sender_id, recipient_id, text, message_type, voice_data, voice_duration_ms, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (sender_id, recipient_id, text, m_type, voice_data, voice_duration, now))
    msg_id = cursor.lastrowid
    conn.commit()
    conn.close()

    payload = {
        'id': msg_id,
        'sender_id': sender_id,
        'recipient_id': recipient_id,
        'text': text,
        'type': m_type,
        'voice_data': voice_data,
        'voice_duration_ms': voice_duration,
        'timestamp': now
    }
    emit('new_message', payload, room=recipient_id)
    emit('message_sent_ack', payload)

@socketio.on('send_bump')
def handle_bump(data):
    sender_id = data.get('sender_id')
    sender_name = data.get('sender_name', 'Ваш друг')
    recipient_id = data.get('recipient_id')
    
    emit('friend_bump', {
        'sender_id': sender_id,
        'sender_name': sender_name,
        'timestamp': int(time.time() * 1000)
    }, room=recipient_id)

# ----------------- REST ENDPOINTS -----------------

@app.route('/health', methods=['GET'])
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'service': 'BFF Location & Social Backend',
        'time': int(time.time() * 1000)
    }), 200

@app.route('/api/users', methods=['GET'])
def get_users():
    conn = get_db()
    users = conn.execute('SELECT * FROM users').fetchall()
    conn.close()
    return jsonify([dict(u) for u in users])

@app.route('/api/users/<user_id>/history', methods=['GET'])
def get_history(user_id):
    conn = get_db()
    points = conn.execute('SELECT * FROM location_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 100', (user_id,)).fetchall()
    conn.close()
    return jsonify([dict(p) for p in points])

@app.route('/api/zones', methods=['GET', 'POST'])
def manage_zones():
    conn = get_db()
    if request.method == 'POST':
        data = request.json or {}
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO safe_zones (name, latitude, longitude, radius_meters, icon, color_hex, notify_entry, notify_exit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (data.get('name'), data.get('latitude'), data.get('longitude'), data.get('radius_meters', 200),
              data.get('icon', '🛡️'), data.get('color_hex', '#10B981'), data.get('notify_entry', 1), data.get('notify_exit', 1)))
        z_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return jsonify({'id': z_id, 'status': 'created'}), 201
    else:
        zones = conn.execute('SELECT * FROM safe_zones').fetchall()
        conn.close()
        return jsonify([dict(z) for z in zones])

@app.route('/api/messages/<user1>/<user2>', methods=['GET'])
def get_chat(user1, user2):
    conn = get_db()
    msgs = conn.execute('''
        SELECT * FROM messages 
        WHERE (sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)
        ORDER BY timestamp ASC
    ''', (user1, user2, user2, user1)).fetchall()
    conn.close()
    return jsonify([dict(m) for m in msgs])

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Запуск BFF Location Server на порту {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
