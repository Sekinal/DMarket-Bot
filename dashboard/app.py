# dashboard/app.py
from flask import Flask, render_template, request, jsonify
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bot import BotManager, DMarketConfig

app = Flask(__name__)
bot_manager = BotManager()

@app.route('/')
def index():
    return render_template('index.html', bots=bot_manager.get_all_bots())

@app.route('/api/bots', methods=['GET'])
def get_bots():
    return jsonify(bot_manager.get_all_bots())

@app.route('/api/bots', methods=['POST'])
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
def remove_bot(instance_id):
    success = bot_manager.remove_bot(instance_id)
    return jsonify({'success': success})

@app.route('/api/bots/<instance_id>/start', methods=['POST'])
def start_bot(instance_id):
    success = bot_manager.start_bot(instance_id)
    return jsonify({'success': success})

@app.route('/api/bots/<instance_id>/stop', methods=['POST'])
def stop_bot(instance_id):
    success = bot_manager.stop_bot(instance_id)
    return jsonify({'success': success})

@app.route('/api/max-prices', methods=['GET'])
def get_max_prices():
    return jsonify({
        'max_prices': bot_manager.max_prices,
        'available_items': list(bot_manager.available_items)
    })

@app.route('/api/max-prices', methods=['POST'])
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