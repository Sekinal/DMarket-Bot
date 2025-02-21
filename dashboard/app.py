# dashboard/app.py
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from functools import wraps
import os
from dotenv import load_dotenv
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bot import BotManager, DMarketConfig
from flask import send_file
import json
from zipfile import ZipFile


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

@app.route('/api/max-prices/<int:index>', methods=['DELETE'])
@login_required
def delete_max_price(index):
    try:
        bot_manager.max_prices.pop(index)
        bot_manager.save_max_prices()
        return jsonify({'success': True})
    except IndexError:
        return jsonify({'success': False, 'error': 'Index not found'}), 404

@app.route('/api/max-prices/<int:index>', methods=['PUT'])
@login_required
def modify_max_price(index):
    try:
        data = request.json
        bot_manager.max_prices[index] = {
            'item': data['item_name'],
            'phase': data.get('phase', ''),
            'float': data.get('float', ''),
            'seed': data.get('seed', ''),
            'price': float(data['max_price'])
        }
        bot_manager.save_max_prices()
        return jsonify({'success': True})
    except IndexError:
        return jsonify({'success': False, 'error': 'Index not found'}), 404

@app.route('/api/export-config', methods=['GET'])
@login_required
def export_config():
    try:
        # Export both bots config and max prices config
        with open(bot_manager.config_file, 'r') as f:
            bots_config = f.read()

        with open(bot_manager.max_prices_file, 'r') as f:
            max_prices_config = f.read()

        # Creating a zip file to contain both config files
        from io import BytesIO
        from zipfile import ZipFile

        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, 'w') as zip_file:
            zip_file.writestr('bots_config.json', bots_config)
            zip_file.writestr('max_prices.json', max_prices_config)

        zip_buffer.seek(0)

        return send_file(zip_buffer, as_attachment=True, download_name='config_files.zip', mimetype='application/zip')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/import-config', methods=['POST'])
@login_required
def import_config():
    try:
        # Ensure the config directory exists
        config_dir = 'config'
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)

        # Get the uploaded file
        uploaded_file = request.files['file']
        if uploaded_file.filename.endswith('.zip'):
            # Save the uploaded zip file in the config directory
            zip_file_path = os.path.join(config_dir, uploaded_file.filename)
            uploaded_file.save(zip_file_path)

            # Unzip the file into the config directory
            with ZipFile(zip_file_path, 'r') as zip_ref:
                zip_ref.extractall(config_dir)

            # Read and load the extracted files
            with open(os.path.join(config_dir, 'bots_config.json'), 'r') as f:
                bots_config = json.load(f)
                bot_manager.bots.clear()  # Clear existing bots
                bot_manager.load_configs()  # Reload from the uploaded config

            with open(os.path.join(config_dir, 'max_prices.json'), 'r') as f:
                max_prices_config = json.load(f)
                bot_manager.max_prices = max_prices_config
                bot_manager.save_max_prices()

            # Cleanup: remove the uploaded zip file after extracting
            os.remove(zip_file_path)

            return jsonify({'success': True})
        else:
            return jsonify({'error': 'Invalid file format, only .zip files are allowed'}), 400

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export-logs', methods=['GET'])
@login_required
def export_logs():
    try:
        log_files = []
        # Gather all log files in the 'logs' directory
        for filename in os.listdir('logs'):
            if filename.endswith('.log'):
                log_files.append(os.path.join('logs', filename))
        
        if not log_files:
            return jsonify({'error': 'No log files found'}), 404

        # Create a zip buffer
        from io import BytesIO
        from zipfile import ZipFile
        
        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, 'w') as zip_file:
            for log_file in log_files:
                zip_file.write(log_file, os.path.basename(log_file))  # Add logs to zip

        zip_buffer.seek(0)  # Go to the beginning of the BytesIO buffer

        return send_file(zip_buffer, as_attachment=True, download_name='logs.zip', mimetype='application/zip')

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run()