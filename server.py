"""
BFF Location Server (Amo / Zenly Backend)
Підтримує:
- Базу даних SQLite (збереження юзерів, історії локацій, чатів, геозон)
- Двосторонній Socket.IO в реальному часі
- Автоматичний глобальний вихід у світову мережу через Cloudflare Tunnel (--tunnel)
- Вбудований Web Dashboard для перегляду карти та друзів у браузері
"""

import os
import sys
import math
import time
import sqlite3
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'bff_super_secret_amo_key_2026')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

DB_NAME = 'bff_location.db'

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Таблиця користувачів
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT,
            avatar_emoji TEXT DEFAULT '😎',
            color_hex TEXT DEFAULT '#8B5CF6',
            latitude REAL DEFAULT 50.4501,
            longitude REAL DEFAULT 30.5234,
            battery_percent INTEGER DEFAULT 100,
            is_charging INTEGER DEFAULT 0,
            speed_kmh INTEGER DEFAULT 0,
            current_activity TEXT DEFAULT 'Онлайн ✨',
            music_title TEXT DEFAULT '',
            music_artist TEXT DEFAULT '',
            last_updated INTEGER,
            is_online INTEGER DEFAULT 1
        )
    ''')

    # Таблиця історії переміщень
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

    # Таблиця повідомлень чату
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            text TEXT,
            message_type TEXT DEFAULT 'text',
            voice_duration_ms INTEGER DEFAULT 0,
            voice_base64 TEXT,
            timestamp INTEGER NOT NULL
        )
    ''')

    # Таблиця зон безпеки
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS safe_zones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            radius_meters REAL DEFAULT 200,
            icon_name TEXT DEFAULT 'home',
            color_hex TEXT DEFAULT '#10B981',
            notify_entry INTEGER DEFAULT 1,
            notify_exit INTEGER DEFAULT 1
        )
    ''')

    # Таблиця сповіщень
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_name TEXT,
            user_id TEXT,
            user_name TEXT,
            event_type TEXT,
            timestamp INTEGER
        )
    ''')

    conn.commit()
    conn.close()

init_db()

# --- Гео-утиліти ---
def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000  # метри
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def check_geofences(user_id, user_name, lat, lon):
    conn = get_db()
    zones = conn.execute('SELECT * FROM safe_zones').fetchall()
    now = int(time.time() * 1000)
    for z in zones:
        dist = haversine_distance(lat, lon, z['latitude'], z['longitude'])
        is_inside = dist <= z['radius_meters']
        last_alert = conn.execute(
            'SELECT event_type FROM alerts WHERE user_id = ? AND zone_name = ? ORDER BY timestamp DESC LIMIT 1',
            (user_id, z['name'])
        ).fetchone()

        last_state = last_alert['event_type'] if last_alert else 'OUT'

        if is_inside and last_state != 'ENTER' and z['notify_entry']:
            conn.execute(
                'INSERT INTO alerts (zone_name, user_id, user_name, event_type, timestamp) VALUES (?, ?, ?, ?, ?)',
                (z['name'], user_id, user_name, 'ENTER', now)
            )
            conn.commit()
            socketio.emit('geofence_alert', {
                'zoneName': z['name'],
                'userId': user_id,
                'userName': user_name,
                'eventType': 'ENTER',
                'timestamp': now
            })
        elif not is_inside and last_state == 'ENTER' and z['notify_exit']:
            conn.execute(
                'INSERT INTO alerts (zone_name, user_id, user_name, event_type, timestamp) VALUES (?, ?, ?, ?, ?)',
                (z['name'], user_id, user_name, 'EXIT', now)
            )
            conn.commit()
            socketio.emit('geofence_alert', {
                'zoneName': z['name'],
                'userId': user_id,
                'userName': user_name,
                'eventType': 'EXIT',
                'timestamp': now
            })
    conn.close()

# --- Socket.IO Події ---
@socketio.on('connect')
def handle_connect():
    print(f"🟢 Клієнт підключився: {request.sid}")
    conn = get_db()
    users = conn.execute('SELECT * FROM users').fetchall()
    zones = conn.execute('SELECT * FROM safe_zones').fetchall()
    conn.close()
    emit('sync_state', {
        'users': [dict(u) for u in users],
        'safeZones': [dict(z) for z in zones]
    })

@socketio.on('disconnect')
def handle_disconnect():
    print(f"🔴 Клієнт відключився: {request.sid}")

@socketio.on('update_location')
def handle_location_update(data):
    user_id = data.get('id', 'me')
    name = data.get('name', 'Користувач')
    username = data.get('username', f'@{user_id}')
    lat = float(data.get('latitude', 50.4501))
    lon = float(data.get('longitude', 30.5234))
    battery = int(data.get('batteryPercent', 100))
    charging = 1 if data.get('isCharging', False) else 0
    speed = int(data.get('speedKmh', 0))
    activity = data.get('currentActivity', 'Онлайн ✨')
    music_title = data.get('currentMusicTitle', '')
    music_artist = data.get('currentMusicArtist', '')
    emoji = data.get('avatarEmoji', '😎')
    color = data.get('colorHex', '#8B5CF6')
    now = int(time.time() * 1000)

    conn = get_db()
    conn.execute('''
        INSERT INTO users (id, name, username, avatar_emoji, color_hex, latitude, longitude,
                           battery_percent, is_charging, speed_kmh, current_activity,
                           music_title, music_artist, last_updated, is_online)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            latitude=excluded.latitude,
            longitude=excluded.longitude,
            battery_percent=excluded.battery_percent,
            is_charging=excluded.is_charging,
            speed_kmh=excluded.speed_kmh,
            current_activity=excluded.current_activity,
            music_title=excluded.music_title,
            music_artist=excluded.music_artist,
            last_updated=excluded.last_updated,
            is_online=1
    ''', (user_id, name, username, emoji, color, lat, lon, battery, charging, speed, activity, music_title, music_artist, now))

    # Збереження точки в історію
    conn.execute('''
        INSERT INTO location_history (user_id, latitude, longitude, speed_kmh, timestamp, label)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, lat, lon, speed, now, activity))
    conn.commit()
    conn.close()

    # Перевірка геозон
    check_geofences(user_id, name, lat, lon)

    # Трансляція всім іншим пристроям
    emit('user_location_changed', {
        'id': user_id,
        'name': name,
        'username': username,
        'avatarEmoji': emoji,
        'colorHex': color,
        'latitude': lat,
        'longitude': lon,
        'batteryPercent': battery,
        'isCharging': bool(charging),
        'speedKmh': speed,
        'currentActivity': activity,
        'currentMusicTitle': music_title,
        'currentMusicArtist': music_artist,
        'lastUpdated': now,
        'isOnline': True
    }, broadcast=True)

@socketio.on('send_message')
def handle_message(data):
    sender_id = data.get('senderId', 'me')
    recipient_id = data.get('recipientId')
    text = data.get('text', '')
    msg_type = data.get('messageType', 'text')
    voice_dur = data.get('voiceDurationMs', 0)
    voice_b64 = data.get('voiceBase64', '')
    now = int(time.time() * 1000)

    conn = get_db()
    conn.execute('''
        INSERT INTO messages (sender_id, recipient_id, text, message_type, voice_duration_ms, voice_base64, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (sender_id, recipient_id, text, msg_type, voice_dur, voice_b64, now))
    conn.commit()
    conn.close()

    emit('new_chat_message', {
        'senderId': sender_id,
        'recipientId': recipient_id,
        'text': text,
        'messageType': msg_type,
        'voiceDurationMs': voice_dur,
        'voiceBase64': voice_b64,
        'timestamp': now
    }, broadcast=True)

# --- REST API Ендпоінти ---
@app.route('/health')
def health():
    return jsonify({"status": "ok", "app": "BFF Location Server", "time": time.time()})

@app.route('/api/users', methods=['GET'])
def get_users():
    conn = get_db()
    users = conn.execute('SELECT * FROM users').fetchall()
    conn.close()
    return jsonify([dict(u) for u in users])

@app.route('/api/history/<user_id>', methods=['GET'])
def get_history(user_id):
    conn = get_db()
    rows = conn.execute('SELECT * FROM location_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 100', (user_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# --- Вбудований Web Dashboard ---
@app.route('/')
@app.route('/dashboard')
def dashboard():
    html = """
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8">
        <title>BFF Location Server — Web Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
        <style>
            body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0E111A; color: #FFF; display: flex; height: 100vh; overflow: hidden; }
            #sidebar { width: 340px; background: #161926; padding: 20px; box-sizing: border-box; display: flex; flex-direction: column; gap: 14px; border-right: 1px solid #232738; }
            #map { flex: 1; height: 100%; }
            .badge { display: inline-block; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; background: #8B5CF6; }
            .user-card { background: #1C2030; padding: 12px; border-radius: 14px; display: flex; align-items: center; gap: 10px; border: 1px solid #2B3045; }
            .avatar { width: 44px; height: 44px; border-radius: 50%; background: #262B40; display: flex; align-items: center; justify-content: center; font-size: 22px; }
        </style>
    </head>
    <body>
        <div id="sidebar">
            <h2>🌍 BFF Location</h2>
            <div style="display:flex; align-items:center; gap:8px;">
                <span id="status-dot" style="width:10px; height:10px; border-radius:50%; background:#10B981;"></span>
                <span id="status-text" style="color:#A0AAB8; font-size:13px;">Підключено до сокету</span>
            </div>
            <h3>Активні друзі</h3>
            <div id="user-list" style="display:flex; flex-direction:column; gap:10px; overflow-y:auto; flex:1;"></div>
        </div>
        <div id="map"></div>
        <script>
            const map = L.map('map').setView([50.4501, 30.5234], 14);
            L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
                attribution: '&copy; OpenStreetMap &copy; CARTO'
            }).addTo(map);

            const markers = {};
            const socket = io();

            socket.on('sync_state', (data) => {
                data.users.forEach(updateUser);
            });

            socket.on('user_location_changed', (user) => {
                updateUser(user);
            });

            function updateUser(user) {
                const list = document.getElementById('user-list');
                let el = document.getElementById('user-' + user.id);
                if (!el) {
                    el = document.createElement('div');
                    el.id = 'user-' + user.id;
                    el.className = 'user-card';
                    list.appendChild(el);
                }
                el.innerHTML = `
                    <div class="avatar">${user.avatar_emoji || user.avatarEmoji || '😎'}</div>
                    <div style="flex:1;">
                        <div style="font-weight:bold;">${user.name}</div>
                        <div style="font-size:12px; color:#A0AAB8;">${user.current_activity || user.currentActivity || 'Онлайн'}</div>
                        <div style="font-size:11px; color:#10B981;">🔋 ${user.battery_percent ?? user.batteryPercent}% ${user.is_charging || user.isCharging ? '⚡' : ''}</div>
                    </div>
                `;

                const lat = user.latitude;
                const lng = user.longitude;
                if (!markers[user.id]) {
                    markers[user.id] = L.marker([lat, lng]).addTo(map).bindPopup(`<b>${user.name}</b><br>${user.current_activity || ''}`);
                } else {
                    markers[user.id].setLatLng([lat, lng]);
                }
            }
        </script>
    </body>
    </html>
    """
    return render_template_string(html)

def start_public_tunnel(port):
    """Створює безкоштовний публічний Cloudflare тунель у глобальну мережу Інтернет."""
    try:
        from pycloudflared import try_cloudflare
        tunnel_url = try_cloudflare(port=port)
        print("\n" + "=" * 65)
        print("🌐 ВАШ СЕРВЕР ВІДКРИТИЙ У СВІТОВУ МЕРЕЖУ (GLOBAL INTERNET):")
        print(f"👉 URL ДЛЯ ДОДАТКУ: {tunnel_url.tunnel}")
        print("Введіть цю адресу в додатку Android у налаштуваннях сервера!")
        print("=" * 65 + "\n")
    except Exception as e:
        print(f"⚠️ Для автоматичного тунелю встановіть 'pip install pycloudflared'. Помилка: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Запуск BFF Location Server на http://0.0.0.0:{port}")

    # Якщо передано флаг --tunnel, запускаємо тунель у глобальну мережу
    if '--tunnel' in sys.argv or os.environ.get('TUNNEL') == '1':
        t = threading.Thread(target=start_public_tunnel, args=(port,), daemon=True)
        t.start()

    socketio.run(app, host='0.0.0.0', port=port, debug=False)
