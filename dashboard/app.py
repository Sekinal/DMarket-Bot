# dashboard/app.py
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from functools import wraps
import os
from dotenv import load_dotenv
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bot import BotManager, DMarketConfig

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
bot_manager = BotManager()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if (username == os.getenv('DASHBOARD_USER') and 
            password == os.getenv('DASHBOARD_PASSWORD')):
            session['logged_in'] = True
            return redirect(url_for('index'))
        return render_template('login.html', error="Invalid credentials")
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html', bots=bot_manager.get_all_bots())

@app.route('/api/bots', methods=['GET'])
@login_required
def get_bots():
    return jsonify(bot_manager.get_all_bots())

@app.route('/api/bots', methods=['POST'])
@login_required
def add_bot():
    data = request.json
    config = DMarketConfig(
        public_key=data['public_key'],
        secret_key=data['secret_key'],
        api_url=data.get('api_url', "https://api.dmarket.com"),
        game_id=data.get('game_id', "a8db"),
        currency=data.get('currency', "USD"),
        check_interval=int(data.get('check_interval', 960))
    )
    success = bot_manager.add_bot(data['instance_id'], config)
    return jsonify({'success': success})

@app.route('/api/bots/<instance_id>', methods=['DELETE'])
@login_required
def remove_bot(instance_id):
    success = bot_manager.remove_bot(instance_id)
    return jsonify({'success': success})

@app.route('/api/bots/<instance_id>/start', methods=['POST'])
@login_required
def start_bot(instance_id):
    success = bot_manager.start_bot(instance_id)
    return jsonify({'success': success})

@app.route('/api/bots/<instance_id>/stop', methods=['POST'])
@login_required
def stop_bot(instance_id):
    success = bot_manager.stop_bot(instance_id)
    return jsonify({'success': success})

@app.route('/api/max-prices', methods=['GET'])
@login_required
def get_max_prices():
    return jsonify({
        'max_prices': bot_manager.max_prices,
        'available_items': list(bot_manager.available_items)
    })

@app.route('/api/max-prices', methods=['POST'])
@login_required
def update_max_price():
    data = request.json
    bot_manager.update_max_price(
        item_name=data['item_name'],
        phase=data.get('phase', ''),
        float_val=data.get('float', ''),
        seed=data.get('seed', ''),
        max_price=float(data['max_price'])
    )
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(debug=True)