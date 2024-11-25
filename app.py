from flask import Flask, render_template, request, jsonify, redirect, url_for, session, abort
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import os
import logging
import geocoder
import requests
from bs4 import BeautifulSoup
from functools import wraps
import secrets
import string
import json
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from hijri_converter import Gregorian
from hijri_converter import convert


# Initialize Flask app
app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Secret key for session management

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///your_database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize Limiter for rate limiting
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["50000 per day", "8000 per hour"]
)

# Admin credentials
ADMIN_USERNAME = 'hade13'
ADMIN_PASSWORD = 'masterhade009'  # Temporary password

# Configure SQLAlchemy
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///logs.db'  # Using SQLite for simplicity
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Set up logging
logging.basicConfig(level=logging.INFO)

# In-memory cache to store prayer times with timestamps
prayer_times_cache = {}
cache_expiry_time = timedelta(hours=23, minutes=10)  # Cache expires after 23 hours and 10 minutes

# API Access Control Flag and credentials
api_enabled = True
api_key = None
api_password = None

def generate_password(length=12):
    """Generate a random password."""
    characters = string.ascii_letters + string.digits 
    return ''.join(secrets.choice(characters) for _ in range(length))

def initialize_api_credentials():
    """Initialize API credentials if not already set."""
    global api_key, api_password
    if not api_key:
        api_key = secrets.token_urlsafe(16)
        logging.info(f"Generated new API key.")
    if not api_password:
        api_password = generate_password()
        logging.info(f"Generated new API password.")

def print_credentials():
    """Print API credentials to the console."""
    global api_key, api_password
    if api_key and api_password:
        print(f"API Key: {api_key}")
        print(f"API Password: {api_password}")

# Define the database models
class APILog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    city = db.Column(db.String(100))
    ip_address = db.Column(db.String(100))
    user_agent = db.Column(db.String(200))

    def __repr__(self):
        return f'<APILog {self.city}>'

class PrayerTimes(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    city = db.Column(db.String(100))
    month = db.Column(db.Integer)
    year = db.Column(db.Integer)
    times = db.Column(db.Text)  # JSON format

    def __repr__(self):
        return f'<PrayerTimes {self.city} {self.month}-{self.year}>'

# Create the database and tables
with app.app_context():
    db.create_all()

# Admin authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            return "Invalid credentials", 403
    return render_template('admin_login.html')

@app.route('/admin/logout')
@login_required
def admin_logout():
    session.pop('logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    logs = APILog.query.order_by(APILog.timestamp.desc()).all()
    initialize_api_credentials()
    return render_template('admin_dashboard.html', logs=logs, api_enabled=api_enabled, api_key=api_key)

@app.route('/admin/toggle_api', methods=['POST'])
@login_required
def toggle_api():
    global api_enabled
    api_enabled = not api_enabled
    status = "enabled" if api_enabled else "disabled"
    logging.info(f"API has been {status} by {ADMIN_USERNAME}.")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/show_api_key', methods=['POST'])
@login_required
def show_api_key():
    initialize_api_credentials()
    return jsonify({'api_key': api_key, 'api_password': api_password})

@app.route('/admin/generate_api_password', methods=['POST'])
@login_required
def generate_api_password():
    global api_password
    api_password = generate_password()
    logging.info(f"Generated new API password.")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/fetch_yearly_prayer_times', methods=['POST'])
def fetch_yearly_prayer_times():
    now = datetime.now()
    year = now.year
    current_month = now.month
    location = geocoder.ip('me')

    if not location.latlng:
        return "Error: Unable to fetch coordinates based on IP address.", 400

    latitude, longitude = location.latlng
    city = location.city if location.city else 'Unknown'

    logging.info(f"Location fetched: {city}, Lat: {latitude}, Lon: {longitude}")

    # Fetch prayer times for the current month and the next month
    for i in range(2):  # Loop for current and next month
        month = (current_month + i - 1) % 12 + 1
        year_to_use = year + (current_month + i - 1) // 12
        logging.info(f"Fetching prayer times for {city} for {month}/{year_to_use}")

        prayer_times = get_prayer_times_for_month(city, latitude, longitude, month, year_to_use)

        if isinstance(prayer_times, str):
            logging.error(f"Error fetching prayer times for {month}/{year_to_use}: {prayer_times}")
            return jsonify({"error": prayer_times}), 500

        # Check if the entry already exists
        existing_entry = PrayerTimes.query.filter_by(city=city, month=month, year=year_to_use).first()
        if existing_entry:
            existing_entry.times = json.dumps(prayer_times)  # Update existing entry
        else:
            prayer_times_entry = PrayerTimes(city=city, month=month, year=year_to_use, times=json.dumps(prayer_times))
            db.session.add(prayer_times_entry)

    db.session.commit()  # Commit the session only once after all additions/updates
    return jsonify({"status": "Prayer times for the current and next month fetched and stored successfully!"}), 200

def get_prayer_times_for_month(city, latitude, longitude, month, year):
    # Generate the appropriate URL for the month
    formatted_month = f"{year}-{month:02d}"  # Format month as YYYY-MM
    url = f"https://prayer-times.muslimpro.com/en/find?coordinates={latitude},{longitude}&country_code=SE&country_name=Sweden&city_name={city}&date={formatted_month}&convention=precalc"

    prayer_times = scrape_prayer_times(url)
    
    if isinstance(prayer_times, str):
        return prayer_times

    monthly_prayer_times = {}
    for date_str, times in prayer_times.items():
        try:
            date_obj = datetime.strptime(date_str, '%a %d %b')
            if date_obj.month == month and date_obj.year == year:
                monthly_prayer_times[date_str] = times
        except ValueError:
            continue

    return monthly_prayer_times

@app.route('/')
@limiter.limit("1000 per minute")  # Rate limit for the homepage
def index():
    if not api_enabled:
        return "API is currently disabled by admin.", 503

    location = geocoder.ip('me')
    if not location.latlng:
        return "Error: Unable to fetch coordinates based on IP address.", 400

    latitude, longitude = location.latlng
    city = location.city if location.city else 'Unknown'

    logging.info(f"Location fetched: {city}, Lat: {latitude}, Lon: {longitude}")

    prayer_times, error = get_prayer_times(city, latitude, longitude)
    if error:
        return prayer_times

    today = datetime.now()
    today_str = today.strftime('%d %b')
    logging.info(f"Today's date: {today_str}")

    today_prayer_times = None
    for date_str, times in prayer_times.items():
        if datetime.strptime(date_str, '%a %d %b').strftime('%d %b') == today_str:
            today_prayer_times = times
            break

    if not today_prayer_times:
        logging.warning("No prayer times found for today.")
        today_prayer_times = {}
    else:
        logging.info(f"Today's prayer times: {today_prayer_times}")

    now = datetime.now()
    logging.info(f"Current time: {now}")

    # Calculate Hijri date
    hijri_date = convert.Gregorian(now.year, now.month, now.day).to_hijri()
    hijri_date_str = f"{hijri_date.day} {hijri_date.month_name()} {hijri_date.year}"

    next_prayer_name = None
    next_prayer_time = None
    current_prayer_name = None
    
    # Check for the next prayer time
    for prayer_name, prayer_time_str in today_prayer_times.items():
        prayer_time = datetime.strptime(prayer_time_str, '%H:%M').replace(year=today.year, month=today.month, day=today.day)
        logging.info(f"Checking prayer: {prayer_name} at {prayer_time}")
        if prayer_time > now:
            next_prayer_name = prayer_name
            next_prayer_time = prayer_time
            logging.info(f"Next prayer found: {next_prayer_name} at {next_prayer_time}")
            break
        else:
            current_prayer_name = prayer_name

    # If there's no next prayer, fetch tomorrow's prayer times
    if not next_prayer_time:
        tomorrow = today + timedelta(days=1)
        logging.info("No upcoming prayers today. Fetching prayer times for tomorrow.")

        # Fetch tomorrow's prayer times
        tomorrow_prayer_times, error = get_prayer_times(city, latitude, longitude)
        if error:
            return tomorrow_prayer_times  # Handle error in fetching tomorrow's times

        tomorrow_str = tomorrow.strftime('%d %b')
        for date_str, times in tomorrow_prayer_times.items():
            if datetime.strptime(date_str, '%a %d %b').strftime('%d %b') == tomorrow_str:
                next_prayer_name = list(times.keys())[0]  # First prayer of tomorrow
                next_prayer_time = datetime.strptime(times[next_prayer_name], '%H:%M').replace(year=tomorrow.year, month=tomorrow.month, day=tomorrow.day)
                break

    if next_prayer_time:
        time_until_next_prayer = next_prayer_time - now
        countdown_seconds = int(time_until_next_prayer.total_seconds())
        next_prayer = {
            "prayer": next_prayer_name,
            "time": next_prayer_time.strftime('%H:%M'),
            "countdown_seconds": countdown_seconds
        }
    else:
        next_prayer = None

    play_asr_sound = (next_prayer_name == "Asr")

    missing_sounds = check_prayer_sounds()
    if missing_sounds:
        logging.warning(f"Missing sound files: {', '.join(missing_sounds)}")
    else:
        logging.info("All required sound files are present.")

    image_folder = os.path.join(app.static_folder, 'images')
    images = [img for img in os.listdir(image_folder) if img.endswith('.jpg')]

    return render_template(
        'index.html', 
        city=city, 
        date=today_str,
        hijri_date=hijri_date,  
        prayer_times=today_prayer_times, 
        next_prayer=next_prayer, 
        current_prayer=current_prayer_name, 
        missing_sounds=missing_sounds,
        images=images,
        play_asr_sound=play_asr_sound,
        countdown_seconds=countdown_seconds if play_asr_sound else None
    )


@app.route('/show_api_credentials')
def show_api_credentials():
    initialize_api_credentials()  # Ensure credentials are generated
    return jsonify({
        'api_key': api_key,
        'api_password': api_password
    })


@app.route('/api/prayer_times', methods=['GET'])
@limiter.limit("100 per minute")  # Rate limit for API requests
def api_prayer_times():
    if not api_enabled:
        return jsonify({"error": "API is currently disabled by admin."}), 503

    api_key_header = request.headers.get('API-Key')
    api_password_header = request.headers.get('API-Password')
    if api_key_header != api_key or api_password_header != api_password:
        return jsonify({"error": "Invalid API key or password."}), 403

    location = geocoder.ip('me')
    if not location.latlng:
        return jsonify({"error": "Unable to fetch coordinates based on IP address."}), 400

    latitude, longitude = location.latlng
    city = location.city if location.city else 'Unknown/Error getting IP'

    logging.info(f"Location fetched for API: {city}, Lat: {latitude}, Lon: {longitude}")

    prayer_times, error = get_prayer_times(city, latitude, longitude)
    if error:
        return jsonify({"error": prayer_times}), 500

    log_api_request(city, request.remote_addr, request.headers.get('User-Agent'))

    return jsonify({"city": city, "prayer_times": prayer_times}), 200

@app.route('/get/json')
def get_prayer_times():
    # Convert prayer times to JSON format
    json_data = json.dumps(PrayerTimes, indent=2)

    # Create a temporary file to store the JSON data
    temp_file_path = 'prayer_times.json'
    with open(temp_file_path, 'w') as temp_file:
        temp_file.write(json_data)

    # Send the JSON file as a response
    return send_file(temp_file_path, as_attachment=True)

@app.route('/status')
def admin_page():
    return render_template('admin.html')

def log_api_request(city, ip_address, user_agent):
    log_entry = APILog(
        city=city,
        ip_address=ip_address,
        user_agent=user_agent
    )
    db.session.add(log_entry)
    db.session.commit()

def get_prayer_times(city, latitude, longitude):
    cache_data = prayer_times_cache.get(city)
    if cache_data and datetime.now() < cache_data['expiry']:
        prayer_times = cache_data['data']
        logging.info("Prayer times fetched from cache.")
    else:
        muslim_pro_url = get_muslim_pro_url(city, latitude, longitude)
        prayer_times = scrape_prayer_times(muslim_pro_url)

        if isinstance(prayer_times, str):
            logging.error(f"Error scraping prayer times: {prayer_times}")
            return prayer_times, True

        prayer_times_cache[city] = {
            'data': prayer_times,
            'expiry': datetime.now() + cache_expiry_time
        }
        logging.info("Prayer times fetched and cached.")

    return prayer_times, False

def check_prayer_sounds():
    required_sounds = ['fajr.mp3', 'dhuhr.mp3', 'asr.mp3', 'maghrib.mp3', 'isha.mp3']
    missing_sounds = []
    sounds_folder = os.path.join(app.static_folder,) 
    for sound in required_sounds:
        sound_path = os.path.join(sounds_folder, sound)
        if not os.path.isfile(sound_path):
            logging.warning(f"Sound file not found: {sound_path}")
            missing_sounds.append(sound)
    return missing_sounds

def get_muslim_pro_url(city, latitude, longitude):
    country_code = 'SE'
    country_name = 'Sweden'

    return f"https://prayer-times.muslimpro.com/en/find?country_code={country_code}&country_name={country_name}&city_name={city}&coordinates={latitude},{longitude}"

def scrape_prayer_times(url):
    try:
        response = requests.get(url)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        table_div = soup.find('div', class_='table-responsive p-0 col-12')
        if not table_div:
            return "Prayer times table div not found!"

        table = table_div.find('table', class_='prayer-times')
        if not table:
            return "Prayer times table not found!"

        rows = []
        tbody = table.find('tbody')
        if tbody:
            for row in tbody.find_all('tr'):
                row_data = [data.text.strip() for data in row.find_all('td')]
                if row_data:
                    rows.append(row_data)

        logging.info(f"Number of rows extracted: {len(rows)}")

        if not rows:
            return "No prayer times found in the table!"

        return format_prayer_times_to_json(rows)

    except requests.RequestException as e:
        return f"Error during requests to {url}: {str(e)}"

def format_prayer_times_to_json(rows):
    prayer_times = {}
    for row in rows:
        date_str = row[0]
        if len(row) >= 7:
            prayer_times[date_str] = {
                "Fajr": row[1],
                "Sunrise": row[2],
                "Dhuhr": row[3],
                "Asr": row[4],
                "Maghrib": row[5],
                "Isha": row[6]
            }
        else:
            logging.warning(f"Skipping row with insufficient data: {row}")

    logging.info(f"Formatted prayer times: {json.dumps(prayer_times, indent=2)}")
    return prayer_times

def get_prayer_times_for_month(city, latitude, longitude, month, year):
    url = get_muslim_pro_url(city, latitude, longitude)
    prayer_times = scrape_prayer_times(url)
    
    if isinstance(prayer_times, str):
        return prayer_times

    monthly_prayer_times = {}
    for date_str, times in prayer_times.items():
        try:
            date_obj = datetime.strptime(date_str, '%a %d %b')
            if date_obj.month == month and date_obj.year == year:
                monthly_prayer_times[date_str] = times
        except ValueError:
            continue

    return monthly_prayer_times

# Create the database and tables
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    initialize_api_credentials()
    print_credentials()
    app.run(host='0.0.0.0', port=414,debug=True)